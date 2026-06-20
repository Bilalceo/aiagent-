"""Live-call smoke mode gate + redaction (A31).

Pure gate/redaction unit tests (deterministic, fake clock) plus WebSocket
integration: smoke mode OFF keeps old behavior, an invalid token is rejected, a
valid token is allowed, and the max-turns hard cap stops the stream safely. No
real network, no real keys.
"""
from __future__ import annotations

import base64

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.core.db import Base, get_session
from app.main import app
from app.models.audit_log import AuditLog
from app.models.call import Call
from app.models.callback_task import CallbackTask
from app.models.knowledge_item import KnowledgeItem
from app.models.telephony_call import TelephonyCall
from app.models.telephony_stream import TelephonyStream
from app.models.transcript import Transcript
from app.services.ai.provider import MockAIProvider
from app.services.ai.service import AIService
from app.services.audit.log import AuditLogService
from app.services.call.session import CallSessionService
from app.services.knowledge.service import KBMatch
from app.services.operator.availability import MockOperatorAvailability, OperatorState
from app.services.operator.transfer import OperatorTransferDecisionService
from app.services.telephony.stream import TelephonyStreamService
from app.services.telephony.twilio import TwilioTelephonyProvider
from app.services.voice.live_call import (
    LiveCallGate,
    redact_number,
    redact_streaming_summary,
)

API = "/api/v1"
WS_URL = f"{API}/telephony/twilio/media-stream"
_WS_PROVIDER = TwilioTelephonyProvider(
    auth_token="ws-secret", public_base_url="https://x", validate_signature=False
)
_TABLES = [
    Call.__table__, Transcript.__table__, AuditLog.__table__, CallbackTask.__table__,
    KnowledgeItem.__table__, TelephonyCall.__table__, TelephonyStream.__table__,
]


# --- gate unit tests ---------------------------------------------------------
def test_gate_off_always_allows() -> None:
    g = LiveCallGate(smoke_mode=False)
    assert g.enabled is False
    assert g.authorize_start(params={}).allowed is True
    assert g.over_limit(turns=999) is None  # no caps when off


def test_gate_requires_valid_token() -> None:
    g = LiveCallGate(smoke_mode=True, require_token=True, smoke_token="s3cret")
    assert g.authorize_start(params={}).allowed is False
    assert g.authorize_start(params={"smoke_token": "wrong"}).allowed is False
    # The token is read ONLY from customParameters (params).
    assert g.authorize_start(params={"smoke_token": "s3cret"}).allowed is True


def test_gate_rejects_when_token_only_in_query_like_kwarg() -> None:
    # authorize_start no longer accepts a query token; passing one is a TypeError,
    # and a params-less start with a required token is rejected.
    g = LiveCallGate(smoke_mode=True, require_token=True, smoke_token="s3cret")
    with pytest.raises(TypeError):
        g.authorize_start(params={}, query_token="s3cret")  # type: ignore[call-arg]
    assert g.authorize_start(params={}).allowed is False


def test_gate_reason_codes_carry_no_token() -> None:
    g = LiveCallGate(smoke_mode=True, smoke_token="s3cret")
    d = g.authorize_start(params={"smoke_token": "leakme"})
    assert d.allowed is False and d.reason == "invalid_smoke_token"
    assert "leakme" not in d.reason and "s3cret" not in d.reason


def test_gate_token_not_required_when_flag_off() -> None:
    g = LiveCallGate(smoke_mode=True, require_token=False)
    assert g.authorize_start(params={}).allowed is True


def test_gate_caller_allowlist() -> None:
    g = LiveCallGate(
        smoke_mode=True, require_token=False, allowed_numbers=["+998901112233"]
    )
    assert g.authorize_start(params={"from_number": "+998901112233"}).allowed is True
    d = g.authorize_start(params={"from_number": "+998900000000"})
    assert d.allowed is False and d.reason == "caller_not_allowed"


def test_gate_max_turns() -> None:
    g = LiveCallGate(smoke_mode=True, require_token=False, max_turns=3, max_duration_seconds=0)
    g.start_clock()
    assert g.over_limit(turns=2) is None
    assert g.over_limit(turns=3) == "live_call_max_turns"


def test_gate_max_duration_with_fake_clock() -> None:
    t = {"now": 100.0}
    g = LiveCallGate(
        smoke_mode=True, require_token=False, max_turns=0,
        max_duration_seconds=180, clock=lambda: t["now"],
    )
    g.start_clock()  # t0 = 100.0
    t["now"] = 279.0
    assert g.over_limit(turns=0) is None  # 179s < 180s
    t["now"] = 281.0
    assert g.over_limit(turns=0) == "live_call_max_duration"  # 181s >= 180s


# --- redaction unit tests ----------------------------------------------------
def test_redact_number() -> None:
    assert redact_number("+998901112233") == "+99" + "*" * 8 + "33"
    assert redact_number("12") == "**"
    assert redact_number("") == "" and redact_number(None) == ""


def test_redact_streaming_summary_keeps_counts_drops_text() -> None:
    summary = {
        "final_count": 1,
        "final_transcripts": [{"text": "patient secret", "language": "uz-UZ", "confidence": 0.9}],
        "turns": [{"transcript_text": "patient secret", "ai_text": "ok", "action": "allow"}],
    }
    redact_streaming_summary(summary)
    assert summary["final_transcripts"][0]["text"] == "[redacted:14]"
    assert summary["final_transcripts"][0]["language"] == "uz-UZ"  # kept
    assert summary["turns"][0]["transcript_text"] == "[redacted:14]"
    assert summary["turns"][0]["ai_text"] == "ok"  # AI reply kept
    assert "patient secret" not in str(summary)


# --- WebSocket integration ---------------------------------------------------
class _StubKnowledge:
    def __init__(self, chunks):
        self._matches = [KBMatch(id=i + 1, title=f"i{i}", content=c, category="faq")
                         for i, c in enumerate(chunks)]

    async def search(self, query, language, intent=None):
        return list(self._matches)


async def _new_call(session, chunks=None):
    css = CallSessionService(
        session,
        AIService(provider=MockAIProvider(), knowledge=_StubKnowledge(chunks or [])),
        AuditLogService(session),
        OperatorTransferDecisionService(
            session, MockOperatorAvailability(OperatorState.AVAILABLE), AuditLogService(session)
        ),
    )
    return (await css.start_call(from_number="+998901112233", to_number="clinic")).call


def _ws_start(stream_sid, call_sid, *, smoke_token=None, from_number=None):
    cp = {
        "call_sid": call_sid,
        "stream_token": _WS_PROVIDER.make_stream_token(call_sid),
        "test_phrase": "Ish vaqtingiz qanday",
    }
    if smoke_token is not None:
        cp["smoke_token"] = smoke_token
    if from_number is not None:
        cp["from_number"] = from_number
    return {
        "event": "start", "streamSid": stream_sid,
        "start": {
            "streamSid": stream_sid, "callSid": call_sid, "tracks": ["inbound"],
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            "customParameters": cp,
        },
    }


def _media(seq):
    return {"event": "media", "sequenceNumber": str(seq),
            "media": {"track": "inbound", "payload": base64.b64encode(b"\x00" * 160).decode()}}


@pytest.fixture
def attach_spy(monkeypatch):
    calls: list[dict] = []
    orig = TelephonyStreamService.attach_streaming_summary

    async def _spy(self, stream, summary):
        calls.append(summary)
        return await orig(self, stream, summary)

    monkeypatch.setattr(TelephonyStreamService, "attach_streaming_summary", _spy)
    return calls


def _seeded_ws(monkeypatch, *, call_sid, smoke_mode, smoke_token="",
               require_token=True, max_turns=10, allowed=""):
    import app.api.v1.telephony as tele

    monkeypatch.setattr(tele, "get_telephony_provider", lambda: _WS_PROVIDER)
    monkeypatch.setattr(settings, "twilio_use_media_streams", True)
    monkeypatch.setattr(settings, "streaming_stt_enabled", True)
    monkeypatch.setattr(settings, "streaming_stt_ai_turns_enabled", True)
    monkeypatch.setattr(settings, "streaming_stt_final_after_frames", 2)
    monkeypatch.setattr(settings, "streaming_tts_enabled", True)
    monkeypatch.setattr(settings, "live_call_smoke_mode", smoke_mode)
    monkeypatch.setattr(settings, "live_call_require_smoke_token", require_token)
    monkeypatch.setattr(settings, "live_call_smoke_token", smoke_token)
    monkeypatch.setattr(settings, "live_call_max_turns", max_turns)
    monkeypatch.setattr(settings, "live_call_allowed_caller_numbers", allowed)

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    state = {"init": False}

    async def _override():
        if not state["init"]:
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_TABLES))
            factory0 = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
            async with factory0() as s0:
                call = await _new_call(s0, chunks=["9:00-18:00"])
                s0.add(TelephonyCall(
                    provider="twilio", provider_call_id=call_sid, call_session_id=call.id,
                    from_number="+998901112233", to_number="clinic", status="in_progress",
                    direction="inbound",
                ))
                await s0.commit()
            state["init"] = True
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    return TestClient(app)


def _drain_to_mark(ws):
    while True:
        if ws.receive_json()["event"] == "mark":
            return


def test_ws_smoke_disabled_keeps_old_behavior(monkeypatch, attach_spy) -> None:
    # No smoke token in the start event, smoke mode OFF -> stream runs as before.
    client = _seeded_ws(monkeypatch, call_sid="CA-off", smoke_mode=False)
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-off", "CA-off"))
            ws.send_json(_media(2))
            ws.send_json(_media(3))
            _drain_to_mark(ws)
            ws.send_json({"event": "stop", "streamSid": "MZ-off", "stop": {}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()
    assert attach_spy[-1]["turn_count"] == 1


def test_ws_smoke_invalid_token_rejected(monkeypatch, attach_spy) -> None:
    client = _seeded_ws(monkeypatch, call_sid="CA-bad", smoke_mode=True, smoke_token="good")
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-bad", "CA-bad", smoke_token="wrong"))
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()  # rejected at start -> closed, no stream
    finally:
        app.dependency_overrides.clear()
    assert attach_spy == []  # no summary attached: stream never started


def test_ws_smoke_valid_token_allows(monkeypatch, attach_spy) -> None:
    client = _seeded_ws(monkeypatch, call_sid="CA-ok", smoke_mode=True, smoke_token="good")
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-ok", "CA-ok", smoke_token="good"))
            ws.send_json(_media(2))
            ws.send_json(_media(3))
            _drain_to_mark(ws)
            ws.send_json({"event": "stop", "streamSid": "MZ-ok", "stop": {}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()
    assert attach_spy[-1]["turn_count"] == 1


def test_ws_smoke_query_token_only_rejected(monkeypatch, attach_spy) -> None:
    # A valid token supplied ONLY in the WSS URL query string is NOT accepted:
    # query tokens leak into proxy logs, so the handler ignores them entirely.
    client = _seeded_ws(monkeypatch, call_sid="CA-q", smoke_mode=True, smoke_token="good")
    try:
        with client.websocket_connect(WS_URL + "?smoke_token=good") as ws:
            ws.send_json(_ws_start("MZ-q", "CA-q"))  # no smoke_token in customParameters
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()  # rejected: token must be in customParameters
    finally:
        app.dependency_overrides.clear()
    assert attach_spy == []  # stream never started


def test_ws_smoke_query_token_not_persisted(monkeypatch, attach_spy) -> None:
    # With a valid customParameters token AND a (red-herring) query token, the
    # stream runs and the query token never appears in persisted metadata.
    client = _seeded_ws(monkeypatch, call_sid="CA-qp", smoke_mode=True, smoke_token="good")
    try:
        with client.websocket_connect(WS_URL + "?smoke_token=QUERYLEAK123") as ws:
            ws.send_json(_ws_start("MZ-qp", "CA-qp", smoke_token="good"))
            ws.send_json(_media(2))
            ws.send_json(_media(3))
            _drain_to_mark(ws)
            ws.send_json({"event": "stop", "streamSid": "MZ-qp", "stop": {}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()
    assert attach_spy[-1]["turn_count"] == 1
    assert "QUERYLEAK123" not in str(attach_spy[-1])  # query token never persisted


def test_ws_smoke_caller_not_allowed_rejected(monkeypatch, attach_spy) -> None:
    client = _seeded_ws(
        monkeypatch, call_sid="CA-cn", smoke_mode=True, require_token=False,
        allowed="+998900000000",
    )
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-cn", "CA-cn", from_number="+998901112233"))
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()
    assert attach_spy == []


def test_ws_smoke_max_turns_guard(monkeypatch, attach_spy) -> None:
    client = _seeded_ws(
        monkeypatch, call_sid="CA-mt", smoke_mode=True, require_token=False, max_turns=1,
    )
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-mt", "CA-mt"))
            ws.send_json(_media(2))
            ws.send_json(_media(3))  # first final -> turn0 plays, then max-turns hits
            _drain_to_mark(ws)
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()  # capped -> stream closed by the guard
    finally:
        app.dependency_overrides.clear()
    assert attach_spy[-1]["stopped_reason"] == "live_call_max_turns"
    assert attach_spy[-1]["turn_count"] == 1

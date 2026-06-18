"""Barge-in + Twilio clear/mark handling (mock-first).

Unit tests for BargeInController (clear-on-speech, mark-completes, idempotency)
and WebSocket integration tests using a SCRIPTED streaming-STT provider so a
playback is active when the caller speaks again. No real Twilio, no paid TTS.
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
from app.services.voice.streaming_stt import (
    StreamingSTTProvider,
    StreamingSTTSession,
    TranscriptEvent,
)
from app.services.voice.streaming_tts import BargeInController

API = "/api/v1"
WS_URL = f"{API}/telephony/twilio/media-stream"

_WS_PROVIDER = TwilioTelephonyProvider(
    auth_token="ws-secret", public_base_url="https://x", validate_signature=False
)
_TABLES = [
    Call.__table__, Transcript.__table__, AuditLog.__table__, CallbackTask.__table__,
    KnowledgeItem.__table__, TelephonyCall.__table__, TelephonyStream.__table__,
]


def _final(text, eid):
    return TranscriptEvent(text=text, language="uz-UZ", is_final=True, provider="mock",
                           confidence=0.9, event_id=eid)


def _partial(text):
    return TranscriptEvent(text=text, language="uz-UZ", is_final=False, provider="mock")


def _playing_summary(mark_name="MZ:turn:0"):
    return {
        "mark_name": mark_name, "status": "playing", "mark_received": False,
        "clear_sent": False, "interrupted": False, "interruption_reason": None,
        "degraded": False,
    }


# --- collecting send sink ----------------------------------------------------
class _CollectSend:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)


# --- unit: BargeInController -------------------------------------------------
def test_begin_playback_ignores_degraded() -> None:
    c = BargeInController(enabled=True)
    c.begin_playback({"degraded": True, "status": "degraded"})
    assert c.active is None
    c.begin_playback(_playing_summary())
    assert c.active is not None


def test_on_mark_completes_matching_playback() -> None:
    c = BargeInController(enabled=True)
    s = _playing_summary("MZ:turn:0")
    c.begin_playback(s)
    c.on_mark("MZ:turn:0")
    assert s["mark_received"] is True and s["status"] == "completed"
    assert c.active is None


def test_on_mark_unknown_name_is_noop() -> None:
    c = BargeInController(enabled=True)
    s = _playing_summary("MZ:turn:0")
    c.begin_playback(s)
    c.on_mark("other")
    c.on_mark(None)
    assert s["mark_received"] is False and s["status"] == "playing"
    assert c.active is s


def test_on_mark_duplicate_is_idempotent() -> None:
    c = BargeInController(enabled=True)
    s = _playing_summary("MZ:turn:0")
    c.begin_playback(s)
    c.on_mark("MZ:turn:0")
    c.on_mark("MZ:turn:0")  # second one is a no-op (active already cleared)
    assert s["status"] == "completed"


@pytest.mark.asyncio
async def test_barge_in_disabled_sends_no_clear() -> None:
    c = BargeInController(enabled=False)
    s = _playing_summary()
    c.begin_playback(s)
    send = _CollectSend()
    assert await c.maybe_barge_in([_partial("salom")], send, "MZ") is False
    assert send.messages == []
    assert s["clear_sent"] is False


@pytest.mark.asyncio
async def test_partial_triggers_single_clear() -> None:
    c = BargeInController(enabled=True)
    s = _playing_summary()
    c.begin_playback(s)
    send = _CollectSend()
    assert await c.maybe_barge_in([_partial("salom")], send, "MZ") is True
    # duplicate caller speech for the same playback -> no second clear
    assert await c.maybe_barge_in([_partial("yana")], send, "MZ") is False
    assert send.messages == [{"event": "clear", "streamSid": "MZ"}]
    assert s["clear_sent"] is True and s["interrupted"] is True
    assert s["status"] == "interrupted" and s["interruption_reason"] == "caller_speech"


@pytest.mark.asyncio
async def test_final_triggers_clear_when_configured() -> None:
    c = BargeInController(enabled=True, on_partial=False, on_final=True)
    s = _playing_summary()
    c.begin_playback(s)
    send = _CollectSend()
    assert await c.maybe_barge_in([_partial("x")], send, "MZ") is False  # partials off
    assert await c.maybe_barge_in([_final("Ha", "f1")], send, "MZ") is True
    assert len(send.messages) == 1


@pytest.mark.asyncio
async def test_min_chars_threshold_ignores_short_speech() -> None:
    c = BargeInController(enabled=True, min_chars=3)
    s = _playing_summary()
    c.begin_playback(s)
    send = _CollectSend()
    assert await c.maybe_barge_in([_partial("a")], send, "MZ") is False  # too short
    assert await c.maybe_barge_in([_partial("salom")], send, "MZ") is True


@pytest.mark.asyncio
async def test_clear_send_failure_marks_degraded_no_raise() -> None:
    async def _boom(_m):
        raise RuntimeError("socket closed")

    c = BargeInController(enabled=True)
    s = _playing_summary()
    c.begin_playback(s)
    assert await c.maybe_barge_in([_partial("salom")], _boom, "MZ") is False
    assert s["degraded"] is True and s["interruption_error"] == "clear_send_error"
    assert s["interrupted"] is True and c.active is None


@pytest.mark.asyncio
async def test_no_active_playback_no_clear() -> None:
    c = BargeInController(enabled=True)
    send = _CollectSend()
    assert await c.maybe_barge_in([_partial("salom")], send, "MZ") is False
    assert send.messages == []


# --- scripted streaming STT provider (per-frame events) ----------------------
class _ScriptedSession(StreamingSTTSession):
    def __init__(self, script: list[list[TranscriptEvent]]) -> None:
        self._script = script
        self._i = 0

    async def accept_audio_frame(self, frame):
        out = self._script[self._i] if self._i < len(self._script) else []
        self._i += 1
        return list(out)

    async def finish_stream(self):
        return []

    async def close(self):
        return None


class _ScriptedProvider(StreamingSTTProvider):
    name = "mock"

    def __init__(self, script):
        self._script = script

    def start_stream(self, context):
        return _ScriptedSession(self._script)


# --- WS harness --------------------------------------------------------------
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


def _ws_start(stream_sid, call_sid):
    return {
        "event": "start", "streamSid": stream_sid,
        "start": {
            "streamSid": stream_sid, "callSid": call_sid, "tracks": ["inbound"],
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            "customParameters": {
                "call_sid": call_sid, "stream_token": _WS_PROVIDER.make_stream_token(call_sid),
            },
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


def _seeded_ws(monkeypatch, *, call_sid, script, barge_enabled, on_partial=True, on_final=True):
    import app.api.deps as depsmod
    import app.api.v1.telephony as tele

    monkeypatch.setattr(tele, "get_telephony_provider", lambda: _WS_PROVIDER)
    monkeypatch.setattr(depsmod, "get_streaming_stt_provider", lambda: _ScriptedProvider(script))
    monkeypatch.setattr(settings, "twilio_use_media_streams", True)
    monkeypatch.setattr(settings, "streaming_stt_enabled", True)
    monkeypatch.setattr(settings, "streaming_stt_ai_turns_enabled", True)
    monkeypatch.setattr(settings, "streaming_tts_enabled", True)
    monkeypatch.setattr(settings, "barge_in_enabled", barge_enabled)
    monkeypatch.setattr(settings, "barge_in_on_partial", on_partial)
    monkeypatch.setattr(settings, "barge_in_on_final", on_final)

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


def _drain(ws):
    """Read every outbound message until the socket closes."""
    msgs = []
    with pytest.raises(WebSocketDisconnect):
        while True:
            msgs.append(ws.receive_json())
    return msgs


# --- WS: barge-in disabled keeps A26 behavior --------------------------------
def test_ws_barge_disabled_sends_no_clear(monkeypatch, attach_spy) -> None:
    script = [[_final("Ish vaqtingiz qanday", "f0")], [_partial("yana savol")]]
    client = _seeded_ws(monkeypatch, call_sid="CA-bd", script=script, barge_enabled=False)
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-bd", "CA-bd"))
            ws.send_json(_media(2))  # final -> turn + playback active
            ws.send_json(_media(3))  # partial -> would barge-in, but disabled
            ws.send_json({"event": "stop", "streamSid": "MZ-bd", "stop": {}})
            msgs = _drain(ws)
    finally:
        app.dependency_overrides.clear()
    assert [m for m in msgs if m["event"] == "clear"] == []  # no clear when disabled
    pb = attach_spy[-1]["turns"][0]["playback"]
    assert pb["clear_sent"] is False and pb["interrupted"] is False


# --- WS: partial during playback triggers exactly one clear ------------------
def test_ws_barge_partial_sends_single_clear(monkeypatch, attach_spy) -> None:
    script = [[_final("Ish vaqtingiz qanday", "f0")], [_partial("yana")], [_partial("yana2")]]
    client = _seeded_ws(monkeypatch, call_sid="CA-bp", script=script, barge_enabled=True)
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-bp", "CA-bp"))
            ws.send_json(_media(2))
            ws.send_json(_media(3))  # partial -> clear
            ws.send_json(_media(4))  # partial again -> NO second clear
            ws.send_json({"event": "stop", "streamSid": "MZ-bp", "stop": {}})
            msgs = _drain(ws)
    finally:
        app.dependency_overrides.clear()
    clears = [m for m in msgs if m["event"] == "clear"]
    assert clears == [{"event": "clear", "streamSid": "MZ-bp"}]  # exactly one
    pb = attach_spy[-1]["turns"][0]["playback"]
    assert pb["clear_sent"] is True and pb["interrupted"] is True
    assert pb["status"] == "interrupted" and pb["interruption_reason"] == "caller_speech"
    # No raw audio / base64 in the persisted metadata.
    assert "MOCK-TTS" not in str(attach_spy[-1]) and "payload" not in str(attach_spy[-1])


# --- WS: final during active playback also clears (then runs its own turn) ----
def test_ws_barge_final_clears_prior_playback(monkeypatch, attach_spy) -> None:
    script = [[_final("Ish vaqtingiz qanday", "f0")], [_final("Manzilingiz qayerda", "f1")]]
    client = _seeded_ws(monkeypatch, call_sid="CA-bf", script=script, barge_enabled=True)
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-bf", "CA-bf"))
            ws.send_json(_media(2))  # final 1 -> turn0 + playback
            ws.send_json(_media(3))  # final 2 -> clears turn0 playback, then turn1
            ws.send_json({"event": "stop", "streamSid": "MZ-bf", "stop": {}})
            msgs = _drain(ws)
    finally:
        app.dependency_overrides.clear()
    assert len([m for m in msgs if m["event"] == "clear"]) == 1
    turns = attach_spy[-1]["turns"]
    assert turns[0]["playback"]["interrupted"] is True  # first playback interrupted
    assert turns[1]["playback"]["mark_name"] == "MZ-bf:turn:1"  # second turn played


# --- WS: clear send failure marks degraded, no crash -------------------------
def test_ws_barge_clear_send_failure_is_safe(monkeypatch, attach_spy) -> None:
    import starlette.websockets as sw

    # Fail ONLY the outbound `clear` send; media/mark of the turn still go through.
    real = sw.WebSocket.send_json

    async def _maybe_fail(self, data, mode="text"):
        if isinstance(data, dict) and data.get("event") == "clear":
            raise RuntimeError("socket closed")
        return await real(self, data, mode)

    monkeypatch.setattr(sw.WebSocket, "send_json", _maybe_fail)

    script = [[_final("Ish vaqtingiz qanday", "f0")], [_partial("yana")]]
    client = _seeded_ws(monkeypatch, call_sid="CA-bg", script=script, barge_enabled=True)
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-bg", "CA-bg"))
            ws.send_json(_media(2))  # final -> turn + playback (media/mark ok)
            ws.send_json(_media(3))  # partial -> clear attempted -> fails safely
            ws.send_json({"event": "stop", "streamSid": "MZ-bg", "stop": {}})
            msgs = _drain(ws)
    finally:
        app.dependency_overrides.clear()
    # The clear never reached the client, but the WS did not crash and persisted.
    assert [m for m in msgs if m["event"] == "clear"] == []
    pb = attach_spy[-1]["turns"][0]["playback"]
    assert pb["degraded"] is True and pb["interruption_error"] == "clear_send_error"
    assert pb["interrupted"] is True


# --- WS: incoming mark completes playback (idempotent / unknown safe) ---------
def test_ws_mark_event_completes_playback(monkeypatch, attach_spy) -> None:
    script = [[_final("Ish vaqtingiz qanday", "f0")]]
    client = _seeded_ws(monkeypatch, call_sid="CA-mk", script=script, barge_enabled=True)
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-mk", "CA-mk"))
            ws.send_json(_media(2))  # final -> turn + playback (mark MZ-mk:turn:0)
            ws.send_json({"event": "mark", "streamSid": "MZ-mk", "mark": {"name": "nope"}})  # unknown
            ws.send_json({"event": "mark", "streamSid": "MZ-mk", "mark": {"name": "MZ-mk:turn:0"}})
            ws.send_json({"event": "mark", "streamSid": "MZ-mk", "mark": {"name": "MZ-mk:turn:0"}})  # dup
            ws.send_json({"event": "stop", "streamSid": "MZ-mk", "stop": {}})
            _drain(ws)
    finally:
        app.dependency_overrides.clear()
    pb = attach_spy[-1]["turns"][0]["playback"]
    assert pb["mark_received"] is True and pb["status"] == "completed"
    assert pb["interrupted"] is False

"""Real streaming STT (Deepgram) adapter (A29).

All tests use a FAKE connection/connector - no network, no Deepgram, no key
required. Covers parsing, the provider session, degrade-on-failure via the session
service, deps fail-fast, and WebSocket-level latency + barge-in driven by fake
Deepgram interim/final events.
"""
from __future__ import annotations

import asyncio
import base64
import json

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
from app.services.voice.deepgram_stt import (
    DeepgramConnection,
    DeepgramConnector,
    DeepgramStreamingSTTProvider,
    WebsocketsDeepgramConnector,
    _WebsocketsConnection,
    parse_deepgram_message,
)
from app.services.voice.streaming_stt import StreamingAudioFrame, StreamingSTTSessionService

API = "/api/v1"
WS_URL = f"{API}/telephony/twilio/media-stream"
_WS_PROVIDER = TwilioTelephonyProvider(
    auth_token="ws-secret", public_base_url="https://x", validate_signature=False
)
_TABLES = [
    Call.__table__, Transcript.__table__, AuditLog.__table__, CallbackTask.__table__,
    KnowledgeItem.__table__, TelephonyCall.__table__, TelephonyStream.__table__,
]


def _dg(text, is_final, confidence=0.9):
    return json.dumps({
        "type": "Results", "is_final": is_final,
        "channel": {"alternatives": [{"transcript": text, "confidence": confidence}]},
    })


# --- fake connection / connector (no network) -------------------------------
class _FakeConn(DeepgramConnection):
    def __init__(self, schedule=None, *, send_error=None, recv_error=None):
        self.sent: list[bytes] = []
        self._schedule = schedule or {}   # int(send_count) | "finish" -> [message_str]
        self._queue: list[str] = []
        self.closed = False
        self._send_error = send_error
        self._recv_error = recv_error

    async def send_audio(self, data):
        if self._send_error:
            raise self._send_error
        self.sent.append(data)
        for m in self._schedule.get(len(self.sent), []):
            self._queue.append(m)

    async def recv(self, *, timeout):
        if self._recv_error:
            raise self._recv_error
        return self._queue.pop(0) if self._queue else None

    async def finish(self):
        for m in self._schedule.get("finish", []):
            self._queue.append(m)

    async def close(self):
        self.closed = True


class _FakeConnector(DeepgramConnector):
    def __init__(self, conn=None, *, connect_error=None):
        self.conn = conn if conn is not None else _FakeConn()
        self._connect_error = connect_error
        self.url = None
        self.headers = None

    async def connect(self, *, url, headers):
        if self._connect_error:
            raise self._connect_error
        self.url, self.headers = url, headers
        return self.conn


def _provider(conn=None, *, connect_error=None, interim=True):
    connector = _FakeConnector(conn, connect_error=connect_error)
    return DeepgramStreamingSTTProvider(api_key="dg-secret", connector=connector,
                                        interim_results=interim), connector


def _frame(payload=b"\x01\x02\x03"):
    return StreamingAudioFrame(stream_sid="MZ", call_sid="CA", sequence_number=1,
                               timestamp_ms=0, payload_bytes=payload)


def _ctx():
    from app.services.voice.streaming_stt import StreamingContext
    return StreamingContext(stream_sid="MZ", call_sid="CA")


# --- parse tests -------------------------------------------------------------
def test_parse_interim_and_final() -> None:
    p = parse_deepgram_message(_dg("hello", False))
    assert p == {"text": "hello", "is_final": False, "confidence": 0.9}
    f = parse_deepgram_message(_dg("hello world", True, 0.8))
    assert f["is_final"] is True and f["text"] == "hello world" and f["confidence"] == 0.8


def test_parse_ignores_empty_and_non_results_and_malformed() -> None:
    assert parse_deepgram_message(_dg("", True)) is None
    assert parse_deepgram_message(_dg("   ", False)) is None
    assert parse_deepgram_message(json.dumps({"type": "Metadata"})) is None
    assert parse_deepgram_message(json.dumps({"type": "SpeechStarted"})) is None
    assert parse_deepgram_message("not json") is None
    assert parse_deepgram_message(json.dumps({"channel": {"alternatives": []}})) is None


def test_parse_caps_length() -> None:
    p = parse_deepgram_message(_dg("x" * 50, True), max_chars=10)
    assert len(p["text"]) == 10


# --- session/provider unit tests --------------------------------------------
@pytest.mark.asyncio
async def test_provider_sends_decoded_bytes_and_uses_auth_header() -> None:
    conn = _FakeConn()
    prov, connector = _provider(conn)
    session = prov.start_stream(_ctx())
    await session.accept_audio_frame(_frame(b"\xaa\xbb"))
    assert conn.sent == [b"\xaa\xbb"]  # decoded audio sent to Deepgram
    assert connector.headers["Authorization"] == "Token dg-secret"  # key in header only
    assert "encoding=mulaw" in connector.url and "sample_rate=8000" in connector.url
    assert "dg-secret" not in connector.url  # key NEVER in URL


@pytest.mark.asyncio
async def test_interim_becomes_partial_event() -> None:
    conn = _FakeConn({1: [_dg("salom", False)]})
    prov, _ = _provider(conn)
    session = prov.start_stream(_ctx())
    events = await session.accept_audio_frame(_frame())
    assert len(events) == 1 and events[0].is_final is False
    assert events[0].provider == "deepgram" and events[0].event_id is None
    assert events[0].text == "salom"


@pytest.mark.asyncio
async def test_final_becomes_final_event_with_id() -> None:
    conn = _FakeConn({1: [_dg("klinika manzili", True, 0.95)]})
    prov, _ = _provider(conn)
    session = prov.start_stream(_ctx())
    events = await session.accept_audio_frame(_frame())
    assert len(events) == 1 and events[0].is_final is True
    assert events[0].event_id == "MZ:dg:0" and events[0].confidence == 0.95


@pytest.mark.asyncio
async def test_empty_and_non_results_ignored_no_degrade() -> None:
    conn = _FakeConn({1: [_dg("", True), json.dumps({"type": "Metadata"}), "bad json"]})
    prov, _ = _provider(conn)
    session = prov.start_stream(_ctx())
    assert await session.accept_audio_frame(_frame()) == []  # all ignored, no raise


@pytest.mark.asyncio
async def test_finish_drains_final() -> None:
    conn = _FakeConn({"finish": [_dg("xayr", True)]})
    prov, _ = _provider(conn)
    session = prov.start_stream(_ctx())
    await session.accept_audio_frame(_frame())  # connect, no events
    finals = await session.finish_stream()
    assert len(finals) == 1 and finals[0].is_final is True


@pytest.mark.asyncio
async def test_interim_disabled_drops_partials() -> None:
    conn = _FakeConn({1: [_dg("salom", False), _dg("salom dunyo", True)]})
    prov, _ = _provider(conn, interim=False)
    session = prov.start_stream(_ctx())
    events = await session.accept_audio_frame(_frame())
    assert [e.is_final for e in events] == [True]  # partial dropped, final kept


# --- degrade-on-failure via the session service ------------------------------
@pytest.mark.asyncio
async def test_connect_failure_degrades_not_crash() -> None:
    prov, _ = _provider(connect_error=RuntimeError("dns"))
    svc = StreamingSTTSessionService(prov)
    await svc.start(stream_sid="MZ")
    assert await svc.push_frame(_frame()) == []
    assert svc.degraded is True and svc.errors >= 1


@pytest.mark.asyncio
async def test_send_failure_degrades_and_closes() -> None:
    conn = _FakeConn(send_error=RuntimeError("broken pipe"))
    prov, _ = _provider(conn)
    svc = StreamingSTTSessionService(prov)
    await svc.start(stream_sid="MZ")
    await svc.push_frame(_frame())
    assert svc.degraded is True and conn.closed is True  # best-effort close on failure


@pytest.mark.asyncio
async def test_recv_failure_degrades() -> None:
    conn = _FakeConn(recv_error=RuntimeError("recv boom"))
    prov, _ = _provider(conn)
    svc = StreamingSTTSessionService(prov)
    await svc.start(stream_sid="MZ")
    await svc.push_frame(_frame())
    assert svc.degraded is True


@pytest.mark.asyncio
async def test_summary_has_no_raw_audio_or_key() -> None:
    conn = _FakeConn({1: [_dg("manzil", True)]})
    prov, _ = _provider(conn)
    svc = StreamingSTTSessionService(prov)
    await svc.start(stream_sid="MZ")
    await svc.push_frame(_frame(b"\x10\x20"))
    s = svc.summary(stopped_reason="stop_event")
    assert s["provider"] == "deepgram" and s["final_count"] == 1
    assert not any(bad in str(s) for bad in ("dg-secret", "payload", "base64", "\\x10"))


# --- deps fail-fast ----------------------------------------------------------
def test_deepgram_missing_key_fails_fast(monkeypatch) -> None:
    from app.api import deps
    monkeypatch.setattr(settings, "streaming_stt_provider", "deepgram")
    monkeypatch.setattr(settings, "deepgram_api_key", "")
    with pytest.raises(RuntimeError, match="DEEPGRAM_API_KEY"):
        deps.get_streaming_stt_provider()


def test_mock_remains_default(monkeypatch) -> None:
    from app.api import deps
    monkeypatch.setattr(settings, "streaming_stt_provider", "mock")
    assert deps.get_streaming_stt_provider().name == "mock"


# --- real connector path (no network: fake websockets connect + fake ws) -----
class _FakeWs:
    def __init__(self, *, recv_exc=None, recv_sleep=False):
        self.sent: list = []
        self.closed = False
        self._recv_exc = recv_exc
        self._recv_sleep = recv_sleep

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if self._recv_sleep:
            await asyncio.sleep(10)  # no message -> caller times out
        if self._recv_exc:
            raise self._recv_exc
        await asyncio.sleep(10)

    async def close(self):
        self.closed = True


class _FakeClosed(Exception):
    pass


@pytest.mark.asyncio
async def test_connector_uses_additional_headers_modern_websockets() -> None:
    captured = {}

    async def fake_connect(uri, *, additional_headers=None, max_size=None):
        captured.update(uri=uri, additional_headers=additional_headers, max_size=max_size)
        return _FakeWs()

    connector = WebsocketsDeepgramConnector(
        connect_timeout=5, recv_timeout=0.05, max_message_bytes=1000, connect_fn=fake_connect
    )
    out = await connector.connect(
        url="wss://api.deepgram.com/v1/listen?model=nova-2&encoding=mulaw",
        headers={"Authorization": "Token dg-secret"},
    )
    assert captured["additional_headers"] == [("Authorization", "Token dg-secret")]
    assert captured["max_size"] == 1000
    assert "dg-secret" not in captured["uri"]  # API key is header-only, never in URL
    assert isinstance(out, _WebsocketsConnection)


@pytest.mark.asyncio
async def test_connector_uses_extra_headers_old_websockets() -> None:
    captured = {}

    async def fake_connect(uri, *, extra_headers=None, max_size=None):
        captured.update(extra_headers=extra_headers)
        return _FakeWs()

    connector = WebsocketsDeepgramConnector(
        connect_timeout=5, recv_timeout=0.05, max_message_bytes=1000, connect_fn=fake_connect
    )
    await connector.connect(url="wss://x", headers={"Authorization": "Token k"})
    assert captured["extra_headers"] == [("Authorization", "Token k")]


@pytest.mark.asyncio
async def test_real_recv_timeout_returns_none() -> None:
    conn = _WebsocketsConnection(_FakeWs(recv_sleep=True), 0.01)
    assert await conn.recv(timeout=0.01) is None  # no message -> None, no raise


@pytest.mark.asyncio
async def test_real_recv_connection_closed_returns_none() -> None:
    conn = _WebsocketsConnection(_FakeWs(recv_exc=_FakeClosed()), 0.05, closed_exc=_FakeClosed)
    assert await conn.recv(timeout=0.05) is None  # closed -> drain ends safely


@pytest.mark.asyncio
async def test_real_recv_unexpected_error_propagates() -> None:
    conn = _WebsocketsConnection(_FakeWs(recv_exc=RuntimeError("proto")), 0.05, closed_exc=_FakeClosed)
    with pytest.raises(RuntimeError):
        await conn.recv(timeout=0.05)  # unexpected error propagates (degrades session)


@pytest.mark.asyncio
async def test_real_recv_error_degrades_session_not_crash() -> None:
    bad_ws = _FakeWs(recv_exc=RuntimeError("recv proto error"))

    class _C(DeepgramConnector):
        async def connect(self, *, url, headers):
            return _WebsocketsConnection(bad_ws, 0.05, closed_exc=_FakeClosed)

    prov = DeepgramStreamingSTTProvider(api_key="k", connector=_C())
    svc = StreamingSTTSessionService(prov)
    await svc.start(stream_sid="MZ")
    assert await svc.push_frame(_frame()) == []
    assert svc.degraded is True and bad_ws.closed is True  # degraded + best-effort close


# --- WebSocket integration (fake Deepgram provider injected) -----------------
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
def latency_spy(monkeypatch):
    calls: list[dict] = []
    orig = TelephonyStreamService.attach_latency_summary

    async def _spy(self, stream, latency):
        calls.append(latency)
        return await orig(self, stream, latency)

    monkeypatch.setattr(TelephonyStreamService, "attach_latency_summary", _spy)
    return calls


@pytest.fixture
def stt_spy(monkeypatch):
    calls: list[dict] = []
    orig = TelephonyStreamService.attach_streaming_summary

    async def _spy(self, stream, summary):
        calls.append(summary)
        return await orig(self, stream, summary)

    monkeypatch.setattr(TelephonyStreamService, "attach_streaming_summary", _spy)
    return calls


def _seeded_ws(monkeypatch, *, call_sid, schedule, barge=False):
    import app.api.deps as depsmod
    import app.api.v1.telephony as tele

    fake_provider = DeepgramStreamingSTTProvider(
        api_key="dg-secret", connector=_FakeConnector(_FakeConn(schedule))
    )
    monkeypatch.setattr(tele, "get_telephony_provider", lambda: _WS_PROVIDER)
    monkeypatch.setattr(depsmod, "get_streaming_stt_provider", lambda: fake_provider)
    monkeypatch.setattr(settings, "twilio_use_media_streams", True)
    monkeypatch.setattr(settings, "streaming_stt_enabled", True)
    monkeypatch.setattr(settings, "streaming_stt_ai_turns_enabled", True)
    monkeypatch.setattr(settings, "streaming_tts_enabled", True)
    monkeypatch.setattr(settings, "streaming_metrics_enabled", True)
    monkeypatch.setattr(settings, "barge_in_enabled", barge)

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


def _recv_until(ws, event_type):
    while True:
        if ws.receive_json()["event"] == event_type:
            return


def _drain(ws):
    with pytest.raises(WebSocketDisconnect):
        while True:
            ws.receive_json()


def test_ws_deepgram_latency_and_turn(monkeypatch, latency_spy, stt_spy) -> None:
    schedule = {1: [_dg("ish", False)], 2: [_dg("ish vaqtingiz qanday", True)]}
    client = _seeded_ws(monkeypatch, call_sid="CA-dg", schedule=schedule)
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-dg", "CA-dg"))
            ws.send_json(_media(2))  # interim -> partial
            ws.send_json(_media(3))  # final -> turn + playback
            _recv_until(ws, "mark")
            ws.send_json({"event": "stop", "streamSid": "MZ-dg", "stop": {}})
            _drain(ws)
    finally:
        app.dependency_overrides.clear()
    summ = stt_spy[-1]
    assert summ["provider"] == "deepgram" and summ["final_count"] == 1
    assert summ["turns"][0]["transcript_text"] == "ish vaqtingiz qanday"
    lat = latency_spy[-1]["events_at_ms"]
    assert "first_partial_transcript_at" in lat and "first_final_transcript_at" in lat
    assert "ai_turn_started_at" in lat
    # No raw audio / base64 / API key anywhere in metadata.
    assert not any(bad in str(summ) + str(latency_spy[-1])
                   for bad in ("dg-secret", "payload", "base64", "MOCK-TTS"))


def test_ws_deepgram_interim_triggers_barge_in(monkeypatch, latency_spy, stt_spy) -> None:
    # frame1 final -> turn + playback; frame2 interim -> barge-in clear.
    schedule = {1: [_dg("ish vaqtingiz qanday", True)], 2: [_dg("yana", False)]}
    client = _seeded_ws(monkeypatch, call_sid="CA-dgb", schedule=schedule, barge=True)
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-dgb", "CA-dgb"))
            ws.send_json(_media(2))  # final -> turn0 + playback
            _recv_until(ws, "mark")
            ws.send_json(_media(3))  # interim -> barge-in clear
            _recv_until(ws, "clear")
            ws.send_json({"event": "stop", "streamSid": "MZ-dgb", "stop": {}})
            _drain(ws)
    finally:
        app.dependency_overrides.clear()
    pb = stt_spy[-1]["turns"][0]["playback"]
    assert pb["clear_sent"] is True and pb["interrupted"] is True
    assert latency_spy[-1]["durations_ms"]["barge_in_clear_latency_ms"] >= 0


@pytest.mark.asyncio
async def test_deepgram_redelivered_final_dedups_in_manager() -> None:
    # A re-delivered SAME final event (same event_id) must NOT double-call the AI.
    from app.services.call.session import MessageOutcome
    from app.services.voice.streaming_turn import StreamingTurnManager, StreamingTurnService

    class _CountingCSS:
        def __init__(self):
            self.calls = 0

        async def handle_message(self, *, call_id, text, language=None):
            self.calls += 1
            return MessageOutcome(reply="ok", action="allow", reason_code="none",
                                  transferred=False, language="uz-UZ")

        async def rollback(self):
            return None

    conn = _FakeConn({1: [_dg("ish vaqtingiz qanday", True)]})
    prov, _ = _provider(conn)
    session = prov.start_stream(_ctx())
    events = await session.accept_audio_frame(_frame())
    final = events[0]
    css = _CountingCSS()
    mgr = StreamingTurnManager(StreamingTurnService(css), call_session_id=1, stream_id=1)
    await mgr.on_final(final)
    await mgr.on_final(final)  # same event_id -> deduped
    assert css.calls == 1 and len(mgr.turns) == 1


def test_ws_deepgram_distinct_finals_create_two_turns(monkeypatch, latency_spy, stt_spy) -> None:
    # Two SEPARATE finals (distinct event_ids) -> two AI turns; no crash.
    schedule = {1: [_dg("ish vaqtingiz qanday", True)], 2: [_dg("manzilingiz qayerda", True)]}
    client = _seeded_ws(monkeypatch, call_sid="CA-dgd", schedule=schedule)
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-dgd", "CA-dgd"))
            ws.send_json(_media(2))
            _recv_until(ws, "mark")
            ws.send_json(_media(3))
            _recv_until(ws, "mark")
            ws.send_json({"event": "stop", "streamSid": "MZ-dgd", "stop": {}})
            _drain(ws)
    finally:
        app.dependency_overrides.clear()
    turns = stt_spy[-1]["turns"]
    assert len(turns) == 2 and turns[0]["transcript_text"] == "ish vaqtingiz qanday"

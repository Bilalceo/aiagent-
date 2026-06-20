"""Real streaming TTS (Deepgram) adapter (A30).

All tests use a FAKE connection/connector - no network, no Deepgram, no key
required. Covers control parsing, the provider synthesize() path (URL/header/raw
bytes), degrade-on-failure via TwilioPlaybackService, the real connector header
shim (no network), deps fail-fast + mock default, and a WebSocket-level run where
the outbound Twilio media decodes back to the exact fake raw audio bytes.
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
from app.services.voice.deepgram_tts import (
    DeepgramStreamingTTSProvider,
    DeepgramTTSConnection,
    DeepgramTTSConnector,
    WebsocketsDeepgramTTSConnector,
    _WebsocketsTTSConnection,
    parse_deepgram_tts_control,
)
from app.services.voice.streaming_tts import TwilioPlaybackService

API = "/api/v1"
WS_URL = f"{API}/telephony/twilio/media-stream"
_WS_PROVIDER = TwilioTelephonyProvider(
    auth_token="ws-secret", public_base_url="https://x", validate_signature=False
)
_TABLES = [
    Call.__table__, Transcript.__table__, AuditLog.__table__, CallbackTask.__table__,
    KnowledgeItem.__table__, TelephonyCall.__table__, TelephonyStream.__table__,
]
_AUDIO = [b"\xaa\xbb\xcc", b"\xdd\xee", b"\xff\x01"]  # fake RAW mu-law frames


# --- fake connection / connector (no network) -------------------------------
class _FakeTTSConn(DeepgramTTSConnection):
    def __init__(self, audio=None, *, control=True, send_error=None,
                 flush_error=None, recv_error=None):
        self.sent_text: list[str] = []
        self.flushed = False
        self.closed = False
        self._queue = list(audio if audio is not None else _AUDIO)
        self._control = control
        self._control_sent = False
        self._send_error = send_error
        self._flush_error = flush_error
        self._recv_error = recv_error

    async def send_text(self, text):
        if self._send_error:
            raise self._send_error
        self.sent_text.append(text)

    async def flush(self):
        if self._flush_error:
            raise self._flush_error
        self.flushed = True

    async def recv(self, *, timeout):
        if self._recv_error:
            raise self._recv_error
        if self._queue:
            return self._queue.pop(0)
        if self._control and not self._control_sent:
            self._control_sent = True
            return json.dumps({"type": "Flushed"})  # ends the drain
        return None

    async def close(self):
        self.closed = True


class _FakeTTSConnector(DeepgramTTSConnector):
    def __init__(self, conn=None, *, connect_error=None):
        self.conn = conn if conn is not None else _FakeTTSConn()
        self._connect_error = connect_error
        self.url = None
        self.headers = None

    async def connect(self, *, url, headers):
        if self._connect_error:
            raise self._connect_error
        self.url, self.headers = url, headers
        return self.conn


def _provider(conn=None, *, connect_error=None):
    connector = _FakeTTSConnector(conn, connect_error=connect_error)
    return DeepgramStreamingTTSProvider(api_key="dg-secret", connector=connector), connector


# --- control parsing ---------------------------------------------------------
def test_parse_control_keeps_only_type() -> None:
    assert parse_deepgram_tts_control(json.dumps({"type": "Flushed"})) == {"type": "Flushed"}
    # A payload-bearing message keeps ONLY the type, never the payload.
    assert parse_deepgram_tts_control(
        json.dumps({"type": "Warning", "description": "secret-ish"})
    ) == {"type": "Warning"}


def test_parse_control_ignores_non_json_and_non_object() -> None:
    assert parse_deepgram_tts_control(b"\x00\x01") is None  # binary audio is not control
    assert parse_deepgram_tts_control("not json") is None
    assert parse_deepgram_tts_control(json.dumps([1, 2])) is None
    assert parse_deepgram_tts_control(json.dumps({"no": "type"})) == {"type": None}


# --- provider synthesize unit tests -----------------------------------------
@pytest.mark.asyncio
async def test_synthesize_sends_text_flush_and_returns_raw_bytes() -> None:
    conn = _FakeTTSConn()
    prov, connector = _provider(conn)
    audio = await prov.synthesize("Salom dunyo", language="uz-UZ")
    assert audio == b"".join(_AUDIO)  # exact raw bytes, concatenated, no header added
    assert conn.sent_text == ["Salom dunyo"] and conn.flushed is True
    assert conn.closed is True  # best-effort close after synth


@pytest.mark.asyncio
async def test_synthesize_uses_auth_header_and_raw_twilio_settings() -> None:
    prov, connector = _provider()
    await prov.synthesize("hi", language="uz-UZ")
    assert connector.headers["Authorization"] == "Token dg-secret"  # key in header only
    assert "dg-secret" not in connector.url  # key NEVER in URL
    assert "encoding=mulaw" in connector.url
    assert "sample_rate=8000" in connector.url
    assert "container=none" in connector.url  # RAW frames, no WAV/RIFF header


@pytest.mark.asyncio
async def test_synthesize_empty_text_does_not_call_provider() -> None:
    prov, connector = _provider()
    assert await prov.synthesize("   ", language="uz-UZ") == b""
    assert connector.url is None and connector.headers is None  # never connected


@pytest.mark.asyncio
async def test_synthesize_ignores_non_audio_control_messages() -> None:
    conn = _FakeTTSConn(audio=[
        json.dumps({"type": "Metadata", "model": "aura"}),
        _AUDIO[0],
        json.dumps({"type": "Warning", "description": "x"}),
        _AUDIO[1],
    ], control=True)
    prov, _ = _provider(conn)
    audio = await prov.synthesize("hi", language="uz-UZ")
    assert audio == _AUDIO[0] + _AUDIO[1]  # only binary frames kept


@pytest.mark.asyncio
async def test_synthesize_honors_aura_voice_override_only() -> None:
    # An Azure-style voice is ignored; an "aura" voice overrides the model.
    prov, connector = _provider()
    await prov.synthesize("hi", language="uz-UZ", voice="uz-UZ-MadinaNeural")
    assert "model=aura-asteria-en" in connector.url  # configured model used
    prov2, connector2 = _provider()
    await prov2.synthesize("hi", language="ru-RU", voice="aura-luna-en")
    assert "model=aura-luna-en" in connector2.url


@pytest.mark.asyncio
async def test_synthesize_caps_text_chars() -> None:
    conn = _FakeTTSConn()
    prov = DeepgramStreamingTTSProvider(
        api_key="k", connector=_FakeTTSConnector(conn), max_chars=5
    )
    await prov.synthesize("x" * 50, language="uz-UZ")
    assert conn.sent_text == ["xxxxx"]  # capped before sending


# --- degrade-on-failure via TwilioPlaybackService ---------------------------
class _CollectSend:
    def __init__(self):
        self.messages: list[dict] = []

    async def __call__(self, message):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_connect_failure_marks_playback_degraded() -> None:
    prov, _ = _provider(connect_error=RuntimeError("dns"))
    svc = TwilioPlaybackService(prov)
    summary = await svc.play(_CollectSend(), stream_sid="MZ", ai_text="hi")
    assert summary["degraded"] is True and summary["error"] == "tts_error"


@pytest.mark.asyncio
async def test_send_failure_marks_playback_degraded_and_closes() -> None:
    conn = _FakeTTSConn(send_error=RuntimeError("broken pipe"))
    prov, _ = _provider(conn)
    summary = await TwilioPlaybackService(prov).play(_CollectSend(), stream_sid="MZ", ai_text="hi")
    assert summary["degraded"] is True and conn.closed is True  # best-effort close


@pytest.mark.asyncio
async def test_flush_failure_marks_playback_degraded() -> None:
    conn = _FakeTTSConn(flush_error=RuntimeError("flush boom"))
    prov, _ = _provider(conn)
    summary = await TwilioPlaybackService(prov).play(_CollectSend(), stream_sid="MZ", ai_text="hi")
    assert summary["degraded"] is True and conn.closed is True


@pytest.mark.asyncio
async def test_receive_failure_marks_playback_degraded() -> None:
    conn = _FakeTTSConn(recv_error=RuntimeError("recv boom"))
    prov, _ = _provider(conn)
    summary = await TwilioPlaybackService(prov).play(_CollectSend(), stream_sid="MZ", ai_text="hi")
    assert summary["degraded"] is True and conn.closed is True


@pytest.mark.asyncio
async def test_playback_summary_provider_deepgram_no_raw_audio() -> None:
    conn = _FakeTTSConn()
    prov, _ = _provider(conn)
    summary = await TwilioPlaybackService(prov, chunk_size=2).play(
        _CollectSend(), stream_sid="MZ", ai_text="hi"
    )
    assert summary["provider"] == "deepgram" and summary["degraded"] is False
    assert summary["bytes_sent"] == len(b"".join(_AUDIO))
    # No raw audio / base64 / key leaks into the persisted summary.
    assert not any(bad in str(summary) for bad in ("dg-secret", "\\xaa", "base64"))


@pytest.mark.asyncio
async def test_twilio_payload_decodes_back_to_raw_bytes_no_double_b64() -> None:
    conn = _FakeTTSConn()
    prov, _ = _provider(conn)
    send = _CollectSend()
    await TwilioPlaybackService(prov, chunk_size=2).play(send, stream_sid="MZ", ai_text="hi")
    medias = [m for m in send.messages if m["event"] == "media"]
    # Decode each payload exactly ONCE -> reconstructs the exact raw audio (no
    # double-base64, no WAV/container header introduced by the provider).
    audio = b"".join(base64.b64decode(m["media"]["payload"]) for m in medias)
    assert audio == b"".join(_AUDIO)


# --- deps fail-fast / mock default ------------------------------------------
def test_deepgram_tts_missing_key_fails_fast(monkeypatch) -> None:
    from app.api import deps
    monkeypatch.setattr(settings, "streaming_tts_provider", "deepgram")
    monkeypatch.setattr(settings, "deepgram_api_key", "")
    with pytest.raises(RuntimeError, match="DEEPGRAM_API_KEY"):
        deps.get_streaming_tts_provider()


def test_streaming_tts_mock_remains_default(monkeypatch) -> None:
    from app.api import deps
    monkeypatch.setattr(settings, "streaming_tts_provider", "mock")
    assert deps.get_streaming_tts_provider().name == "mock"


# --- real connector header shim (no network) --------------------------------
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
            await asyncio.sleep(10)
        if self._recv_exc:
            raise self._recv_exc
        await asyncio.sleep(10)

    async def close(self):
        self.closed = True


class _FakeClosed(Exception):
    pass


@pytest.mark.asyncio
async def test_real_connector_uses_additional_headers_and_key_not_in_url() -> None:
    captured = {}

    async def fake_connect(uri, *, additional_headers=None, max_size=None):
        captured.update(uri=uri, additional_headers=additional_headers, max_size=max_size)
        return _FakeWs()

    connector = WebsocketsDeepgramTTSConnector(
        connect_timeout=5, recv_timeout=5, max_message_bytes=1000, connect_fn=fake_connect
    )
    out = await connector.connect(
        url="wss://api.deepgram.com/v1/speak?encoding=mulaw&container=none",
        headers={"Authorization": "Token dg-secret"},
    )
    assert captured["additional_headers"] == [("Authorization", "Token dg-secret")]
    assert captured["max_size"] == 1000
    assert "dg-secret" not in captured["uri"]  # key header-only, never in URL
    assert isinstance(out, _WebsocketsTTSConnection)


@pytest.mark.asyncio
async def test_real_connection_send_text_and_flush_messages() -> None:
    ws = _FakeWs()
    conn = _WebsocketsTTSConnection(ws, 5.0, closed_exc=_FakeClosed)
    await conn.send_text("hi")
    await conn.flush()
    assert json.loads(ws.sent[0]) == {"type": "Speak", "text": "hi"}
    assert json.loads(ws.sent[1]) == {"type": "Flush"}


@pytest.mark.asyncio
async def test_real_recv_timeout_returns_none() -> None:
    conn = _WebsocketsTTSConnection(_FakeWs(recv_sleep=True), 0.01)
    assert await conn.recv(timeout=0.01) is None


@pytest.mark.asyncio
async def test_real_recv_connection_closed_returns_none() -> None:
    conn = _WebsocketsTTSConnection(_FakeWs(recv_exc=_FakeClosed()), 0.05, closed_exc=_FakeClosed)
    assert await conn.recv(timeout=0.05) is None


@pytest.mark.asyncio
async def test_real_recv_unexpected_error_propagates() -> None:
    conn = _WebsocketsTTSConnection(_FakeWs(recv_exc=RuntimeError("proto")), 0.05, closed_exc=_FakeClosed)
    with pytest.raises(RuntimeError):
        await conn.recv(timeout=0.05)


# --- WebSocket integration (fake Deepgram TTS provider injected) ------------
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
                "test_phrase": "Ish vaqtingiz qanday",
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


def _seeded_ws(monkeypatch, *, call_sid):
    import app.api.deps as depsmod
    import app.api.v1.telephony as tele

    fake_provider = DeepgramStreamingTTSProvider(
        api_key="dg-secret", connector=_FakeTTSConnector(_FakeTTSConn())
    )
    monkeypatch.setattr(tele, "get_telephony_provider", lambda: _WS_PROVIDER)
    monkeypatch.setattr(depsmod, "get_streaming_tts_provider", lambda: fake_provider)
    monkeypatch.setattr(settings, "twilio_use_media_streams", True)
    monkeypatch.setattr(settings, "streaming_stt_enabled", True)
    monkeypatch.setattr(settings, "streaming_stt_ai_turns_enabled", True)
    monkeypatch.setattr(settings, "streaming_stt_final_after_frames", 2)
    monkeypatch.setattr(settings, "streaming_tts_enabled", True)
    monkeypatch.setattr(settings, "streaming_tts_chunk_bytes", 2)  # several media frames

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


def _drain_playback(ws):
    payloads = []
    while True:
        m = ws.receive_json()
        if m["event"] == "mark":
            return payloads, m
        assert m["event"] == "media"
        payloads.append(m["media"]["payload"])


def test_ws_deepgram_tts_media_decodes_to_raw_bytes(monkeypatch, attach_spy) -> None:
    client = _seeded_ws(monkeypatch, call_sid="CA-tts")
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-tts", "CA-tts"))
            ws.send_json(_media(2))
            ws.send_json(_media(3))  # final -> AI turn -> Deepgram TTS playback
            payloads, mark = _drain_playback(ws)
            ws.send_json({"event": "stop", "streamSid": "MZ-tts", "stop": {}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()
    # Outbound Twilio media decodes back to EXACTLY the fake raw audio bytes.
    audio = b"".join(base64.b64decode(p) for p in payloads)
    assert audio == b"".join(_AUDIO)
    assert mark["mark"]["name"] == "MZ-tts:turn:0"
    pb = attach_spy[-1]["turns"][0]["playback"]
    assert pb["provider"] == "deepgram" and pb["degraded"] is False
    assert pb["bytes_sent"] == len(b"".join(_AUDIO))
    # No raw audio / base64 / key anywhere in persisted metadata.
    assert not any(bad in str(attach_spy[-1]) for bad in ("dg-secret", "payload", "base64"))

"""Streaming TTS playback (mock-first) over Twilio Media Streams.

Unit tests for the Twilio outbound event builders + TwilioPlaybackService, and
WebSocket integration tests that drive a final transcript -> AI turn -> outbound
mock playback (media + mark). No real Twilio, no paid TTS, no raw audio in
metadata.
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
from app.services.voice.streaming_tts import (
    MockStreamingTTSProvider,
    StreamingTTSProvider,
    TwilioPlaybackService,
    build_clear_message,
    build_mark_message,
    build_media_message,
    chunk_bytes,
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


# --- unit: builders + chunking ----------------------------------------------
def test_media_message_shape() -> None:
    m = build_media_message("MZ1", "Zm9v")
    assert m == {"event": "media", "streamSid": "MZ1", "media": {"payload": "Zm9v"}}


def test_mark_and_clear_message_shape() -> None:
    assert build_mark_message("MZ1", "t-0") == {
        "event": "mark", "streamSid": "MZ1", "mark": {"name": "t-0"}
    }
    assert build_clear_message("MZ1") == {"event": "clear", "streamSid": "MZ1"}


def test_chunk_bytes_splits_and_bounds() -> None:
    assert chunk_bytes(b"abcdef", 2) == [b"ab", b"cd", b"ef"]
    assert chunk_bytes(b"abcde", 2) == [b"ab", b"cd", b"e"]
    assert chunk_bytes(b"", 2) == []


# --- unit: TwilioPlaybackService --------------------------------------------
class _CollectSend:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)


class _BoomProvider(StreamingTTSProvider):
    name = "boom"

    async def synthesize(self, text, *, language, voice=None) -> bytes:
        raise RuntimeError("tts down")


@pytest.mark.asyncio
async def test_playback_sends_media_then_mark() -> None:
    svc = TwilioPlaybackService(MockStreamingTTSProvider(), chunk_size=8)
    send = _CollectSend()
    summary = await svc.play(send, stream_sid="MZ1", ai_text="Salom dunyo", turn_order=0)

    medias = [m for m in send.messages if m["event"] == "media"]
    marks = [m for m in send.messages if m["event"] == "mark"]
    assert len(marks) == 1 and send.messages[-1]["event"] == "mark"
    assert marks[0]["mark"]["name"] == "MZ1:turn:0"
    assert len(medias) == summary["chunks_sent"] >= 1
    # Each payload is valid base64 and reconstructs the deterministic mock audio.
    audio = b"".join(base64.b64decode(m["media"]["payload"]) for m in medias)
    assert audio == b"MOCK-TTS:" + "Salom dunyo".encode("utf-8")
    assert summary["degraded"] is False and summary["error"] is None
    assert summary["bytes_sent"] == len(audio)


@pytest.mark.asyncio
async def test_playback_empty_text_is_degraded() -> None:
    svc = TwilioPlaybackService(MockStreamingTTSProvider())
    send = _CollectSend()
    summary = await svc.play(send, stream_sid="MZ1", ai_text="   ")
    assert summary["degraded"] is True and summary["error"] == "empty_text"
    assert send.messages == []  # nothing sent


@pytest.mark.asyncio
async def test_playback_provider_error_is_degraded() -> None:
    svc = TwilioPlaybackService(_BoomProvider())
    send = _CollectSend()
    summary = await svc.play(send, stream_sid="MZ1", ai_text="Salom")
    assert summary["degraded"] is True and summary["error"] == "tts_error"
    assert send.messages == []


@pytest.mark.asyncio
async def test_playback_send_error_is_degraded() -> None:
    async def _bad_send(_message):
        raise RuntimeError("socket closed")

    svc = TwilioPlaybackService(MockStreamingTTSProvider(), chunk_size=4)
    summary = await svc.play(_bad_send, stream_sid="MZ1", ai_text="Salom")
    assert summary["degraded"] is True and summary["error"] == "send_error"


@pytest.mark.asyncio
async def test_playback_over_max_chunks_truncates_safely() -> None:
    svc = TwilioPlaybackService(MockStreamingTTSProvider(), chunk_size=1, max_chunks=3)
    send = _CollectSend()
    summary = await svc.play(send, stream_sid="MZ1", ai_text="abcdefghij")
    assert summary["chunks_sent"] == 3  # capped
    assert summary["truncated"] is True


@pytest.mark.asyncio
async def test_playback_over_max_text_truncates_safely() -> None:
    svc = TwilioPlaybackService(MockStreamingTTSProvider(), chunk_size=1000, max_text_chars=5)
    send = _CollectSend()
    summary = await svc.play(send, stream_sid="MZ1", ai_text="x" * 50)
    assert summary["truncated"] is True
    audio = base64.b64decode(send.messages[0]["media"]["payload"])
    assert audio == b"MOCK-TTS:" + b"x" * 5  # only the capped text was synthesized


@pytest.mark.asyncio
async def test_playback_summary_has_no_raw_audio() -> None:
    svc = TwilioPlaybackService(MockStreamingTTSProvider())
    summary = await svc.play(_CollectSend(), stream_sid="MZ", ai_text="hi")
    fields = {"provider", "enabled", "voice", "chunks_sent", "bytes_sent", "mark_name",
              "truncated", "degraded", "error"}
    assert set(summary) == fields
    assert not any(bad in k for k in summary for bad in ("payload", "audio", "base64"))


# --- WebSocket integration --------------------------------------------------
class _StubKnowledge:
    def __init__(self, chunks: list[str]) -> None:
        self._matches = [
            KBMatch(id=i + 1, title=f"item-{i + 1}", content=c, category="faq")
            for i, c in enumerate(chunks)
        ]

    async def search(self, query, language, intent=None) -> list[KBMatch]:
        return list(self._matches)


async def _new_call(session: AsyncSession, chunks=None) -> Call:
    css = CallSessionService(
        session,
        AIService(provider=MockAIProvider(), knowledge=_StubKnowledge(chunks or [])),
        AuditLogService(session),
        OperatorTransferDecisionService(
            session, MockOperatorAvailability(OperatorState.AVAILABLE), AuditLogService(session)
        ),
    )
    return (await css.start_call(from_number="+998901112233", to_number="clinic")).call


def _ws_start(stream_sid, call_sid, phrase) -> dict:
    cp = {
        "call_sid": call_sid,
        "stream_token": _WS_PROVIDER.make_stream_token(call_sid),
        "test_phrase": phrase,
    }
    return {
        "event": "start", "sequenceNumber": "1", "streamSid": stream_sid,
        "start": {
            "streamSid": stream_sid, "callSid": call_sid, "tracks": ["inbound"],
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            "customParameters": cp,
        },
    }


def _media(payload, seq="2") -> dict:
    return {"event": "media", "sequenceNumber": seq, "media": {"track": "inbound", "payload": payload}}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@pytest.fixture
def attach_spy(monkeypatch):
    calls: list[dict] = []
    orig = TelephonyStreamService.attach_streaming_summary

    async def _spy(self, stream, summary):
        calls.append(summary)
        return await orig(self, stream, summary)

    monkeypatch.setattr(TelephonyStreamService, "attach_streaming_summary", _spy)
    return calls


def _seeded_ws(monkeypatch, *, call_sid, tts_enabled, chunks=None):
    """Build a TestClient with a seeded CallSession + linked TelephonyCall."""
    import app.api.v1.telephony as tele

    monkeypatch.setattr(tele, "get_telephony_provider", lambda: _WS_PROVIDER)
    monkeypatch.setattr(settings, "twilio_use_media_streams", True)
    monkeypatch.setattr(settings, "streaming_stt_enabled", True)
    monkeypatch.setattr(settings, "streaming_stt_ai_turns_enabled", True)
    monkeypatch.setattr(settings, "streaming_stt_final_after_frames", 2)
    monkeypatch.setattr(settings, "streaming_tts_enabled", tts_enabled)

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
                call = await _new_call(s0, chunks=chunks)
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
    """Read outbound media frames until the trailing mark; return (payloads, mark)."""
    payloads = []
    while True:
        m = ws.receive_json()
        if m["event"] == "mark":
            return payloads, m
        assert m["event"] == "media"
        payloads.append(m["media"]["payload"])


def test_ws_tts_disabled_sends_no_outbound_and_keeps_turn(monkeypatch, attach_spy) -> None:
    client = _seeded_ws(monkeypatch, call_sid="CA-off", tts_enabled=False, chunks=["9:00-18:00"])
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-off", "CA-off", phrase="Ish vaqtingiz qanday"))
            ws.send_json(_media(_b64(b"\x00" * 160)))
            ws.send_json(_media(_b64(b"\x00" * 160), seq="3"))
            ws.send_json({"event": "stop", "streamSid": "MZ-off", "stop": {}})
            # No outbound playback -> the next receive is the close, not a media event.
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()
    s = attach_spy[-1]
    assert s["turn_count"] == 1
    assert "playback" not in s["turns"][0]  # A25 behavior intact, no playback block


def test_ws_tts_enabled_streams_media_and_mark(monkeypatch, attach_spy) -> None:
    monkeypatch.setattr(settings, "streaming_tts_chunk_bytes", 16)  # force several frames
    client = _seeded_ws(monkeypatch, call_sid="CA-on", tts_enabled=True, chunks=["9:00-18:00"])
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-on", "CA-on", phrase="Ish vaqtingiz qanday"))
            ws.send_json(_media(_b64(b"\x00" * 160)))
            ws.send_json(_media(_b64(b"\x00" * 160), seq="3"))
            payloads, mark = _drain_playback(ws)
            ws.send_json({"event": "stop", "streamSid": "MZ-on", "stop": {}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()
    assert len(payloads) >= 1
    audio = b"".join(base64.b64decode(p) for p in payloads)  # each payload is valid base64
    assert audio.startswith(b"MOCK-TTS:")
    assert mark["mark"]["name"] == "MZ-on:turn:0"
    pb = attach_spy[-1]["turns"][0]["playback"]
    assert pb["chunks_sent"] == len(payloads)
    assert pb["bytes_sent"] == len(audio)
    assert pb["degraded"] is False and pb["mark_name"] == "MZ-on:turn:0"
    # No raw audio / base64 in the persisted summary.
    assert "MOCK-TTS" not in str(attach_spy[-1])
    assert "payload" not in str(attach_spy[-1]) and "base64" not in str(attach_spy[-1])


def test_ws_tts_emergency_plays_safe_103_text(monkeypatch, attach_spy) -> None:
    client = _seeded_ws(monkeypatch, call_sid="CA-emrg", tts_enabled=True)
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-emrg", "CA-emrg", phrase="Nafas ololmayapman"))
            ws.send_json(_media(_b64(b"\x00" * 160)))
            ws.send_json(_media(_b64(b"\x00" * 160), seq="3"))
            payloads, _mark = _drain_playback(ws)
            ws.send_json({"event": "stop", "streamSid": "MZ-emrg", "stop": {}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()
    audio = b"".join(base64.b64decode(p) for p in payloads)
    assert b"103" in audio  # the official emergency message was the spoken text
    turn = attach_spy[-1]["turns"][0]
    assert turn["action"] == "emergency" and turn["playback"]["degraded"] is False


def test_ws_tts_operator_transfer_plays_safe_text(monkeypatch, attach_spy) -> None:
    # Factual question + empty KB -> operator transfer; the safe operator reply is played.
    client = _seeded_ws(monkeypatch, call_sid="CA-op", tts_enabled=True, chunks=[])
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-op", "CA-op", phrase="Kardiolog qabuli narxi qancha"))
            ws.send_json(_media(_b64(b"\x00" * 160)))
            ws.send_json(_media(_b64(b"\x00" * 160), seq="3"))
            payloads, _mark = _drain_playback(ws)
            ws.send_json({"event": "stop", "streamSid": "MZ-op", "stop": {}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()
    audio = b"".join(base64.b64decode(p) for p in payloads)
    assert b"operator" in audio  # uz safe reply: "...sizni operatorga ulayman."
    turn = attach_spy[-1]["turns"][0]
    assert turn["transferred"] is True and turn["playback"]["chunks_sent"] >= 1


def test_ws_tts_provider_error_marks_degraded_no_crash(monkeypatch, attach_spy) -> None:
    import app.api.deps as depsmod

    monkeypatch.setattr(depsmod, "get_streaming_tts_provider", lambda: _BoomProvider())
    client = _seeded_ws(monkeypatch, call_sid="CA-err", tts_enabled=True, chunks=["9:00-18:00"])
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-err", "CA-err", phrase="Ish vaqtingiz qanday"))
            ws.send_json(_media(_b64(b"\x00" * 160)))
            ws.send_json(_media(_b64(b"\x00" * 160), seq="3"))
            # Synthesis fails before any media -> no outbound; stop then disconnect.
            ws.send_json({"event": "stop", "streamSid": "MZ-err", "stop": {}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()
    pb = attach_spy[-1]["turns"][0]["playback"]
    assert pb["degraded"] is True and pb["error"] == "tts_error"
    assert pb["chunks_sent"] == 0

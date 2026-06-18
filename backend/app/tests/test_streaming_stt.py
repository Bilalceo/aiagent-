"""Streaming STT (mock-first) for Twilio Media Streams.

Unit tests for the session service + mock provider, a service-level persistence
test through the admin detail endpoint, and WebSocket integration tests that
assert the attached summary via a spy (no cross-loop DB reads, no real audio).
"""
from __future__ import annotations

import base64
import time

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.core.db import Base, get_session
from app.main import app
from app.models.call import Call
from app.models.telephony_call import TelephonyCall
from app.models.telephony_stream import TelephonyStream
from app.services.auth.service import AuthService
from app.services.telephony.stream import TelephonyStreamService
from app.services.telephony.twilio import TwilioTelephonyProvider
from app.services.voice.streaming_stt import (
    MockStreamingSTTProvider,
    StreamingAudioFrame,
    StreamingSTTProvider,
    StreamingSTTSession,
    StreamingSTTSessionService,
)

API = "/api/v1"
WS_URL = f"{API}/telephony/twilio/media-stream"

_WS_PROVIDER = TwilioTelephonyProvider(
    auth_token="ws-secret", public_base_url="https://x", validate_signature=False
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _frame(n_bytes: int = 160) -> StreamingAudioFrame:
    return StreamingAudioFrame(
        stream_sid="MZ", call_sid="CA", sequence_number=1, timestamp_ms=0,
        payload_bytes=b"\x00" * n_bytes,
    )


def _ws_start(stream_sid="MZ", call_sid="CA", phrase=None) -> dict:
    cp = {"call_sid": call_sid, "stream_token": _WS_PROVIDER.make_stream_token(call_sid)}
    if phrase is not None:
        cp["test_phrase"] = phrase
    return {
        "event": "start", "sequenceNumber": "1", "streamSid": stream_sid,
        "start": {
            "streamSid": stream_sid, "callSid": call_sid, "tracks": ["inbound"],
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            "customParameters": cp,
        },
    }


def _media(payload: str, seq="2") -> dict:
    return {"event": "media", "sequenceNumber": seq, "media": {"track": "inbound", "payload": payload}}


# --- failing provider (degraded path) ---------------------------------------
class _BoomSession(StreamingSTTSession):
    async def accept_audio_frame(self, frame):
        raise RuntimeError("boom")

    async def finish_stream(self):
        return []

    async def close(self):
        return None


class _BoomProvider(StreamingSTTProvider):
    name = "boom"

    def start_stream(self, context):
        return _BoomSession()


# --- unit: StreamingSTTSessionService + mock provider -----------------------
@pytest.mark.asyncio
async def test_mock_emits_partial_then_final() -> None:
    svc = StreamingSTTSessionService(MockStreamingSTTProvider(final_after_frames=4))
    await svc.start(stream_sid="MZ", call_sid="CA", params={"test_phrase": "Klinika manzili qayerda"})
    events = []
    for _ in range(4):
        events += await svc.push_frame(_frame())
    partials = [e for e in events if not e.is_final]
    finals = [e for e in events if e.is_final]
    assert len(partials) >= 1  # partial before final
    assert len(finals) == 1
    assert finals[0].text == "Klinika manzili qayerda"
    assert svc.final_transcript.text == "Klinika manzili qayerda"
    assert svc.partial_count >= 1


@pytest.mark.asyncio
async def test_partial_does_not_finalize_early() -> None:
    svc = StreamingSTTSessionService(MockStreamingSTTProvider(final_after_frames=6))
    await svc.start(stream_sid="MZ")
    for _ in range(3):  # reaches the partial threshold, not the final one
        await svc.push_frame(_frame())
    assert svc.final_transcript is None  # not final -> no AI turn would be triggered
    assert svc.partial_count >= 1


@pytest.mark.asyncio
async def test_finish_emits_final_if_none() -> None:
    svc = StreamingSTTSessionService(MockStreamingSTTProvider(final_after_frames=100))
    await svc.start(stream_sid="MZ", params={"test_phrase": "Salom"})
    await svc.push_frame(_frame())
    assert svc.final_transcript is None
    finals = await svc.finish()
    assert len(finals) == 1 and finals[0].is_final
    assert svc.final_transcript.text == "Salom"


@pytest.mark.asyncio
async def test_over_max_frames_sets_over_limit() -> None:
    svc = StreamingSTTSessionService(MockStreamingSTTProvider(final_after_frames=100), max_frames=2)
    await svc.start(stream_sid="MZ")
    await svc.push_frame(_frame())
    await svc.push_frame(_frame())
    await svc.push_frame(_frame())  # exceeds the cap
    assert svc.over_limit is True


@pytest.mark.asyncio
async def test_over_max_bytes_sets_over_limit() -> None:
    svc = StreamingSTTSessionService(MockStreamingSTTProvider(final_after_frames=100), max_bytes=300)
    await svc.start(stream_sid="MZ")
    await svc.push_frame(_frame(200))
    await svc.push_frame(_frame(200))  # 400 > 300
    assert svc.over_limit is True


@pytest.mark.asyncio
async def test_provider_error_marks_degraded() -> None:
    svc = StreamingSTTSessionService(_BoomProvider())
    await svc.start(stream_sid="MZ")
    await svc.push_frame(_frame())
    assert svc.degraded is True
    assert svc.errors >= 1
    assert svc.summary(stopped_reason="error")["degraded"] is True


@pytest.mark.asyncio
async def test_empty_payload_frame_is_safe() -> None:
    svc = StreamingSTTSessionService(MockStreamingSTTProvider(final_after_frames=2))
    await svc.start(stream_sid="MZ")
    await svc.push_frame(_frame(0))  # empty payload (e.g. malformed base64 upstream)
    await svc.push_frame(_frame(0))
    assert svc.final_transcript is not None  # still progresses, no crash


def test_summary_has_no_raw_audio_keys() -> None:
    svc = StreamingSTTSessionService(MockStreamingSTTProvider())
    s = svc.summary()
    assert not any(bad in key for key in s for bad in ("payload", "audio", "base64", "raw"))
    assert {"frames_processed", "bytes_processed", "final_transcripts", "stopped_reason"} <= set(s)


# --- service-level persistence + admin detail -------------------------------
@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    async def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _bh(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _admin_token(client, db_session) -> str:
    await AuthService(db_session).create_user(
        email="admin@clinic.uz", password="rolepass001", full_name="admin", role="admin"
    )
    await db_session.commit()
    r = await client.post(
        f"{API}/auth/login", json={"email": "admin@clinic.uz", "password": "rolepass001"}
    )
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_final_transcript_persisted_and_in_detail(client, db_session) -> None:
    token = await _admin_token(client, db_session)
    tsvc = TelephonyStreamService(db_session)
    stream = await tsvc.start_stream(_ws_start("MZ-p", "CA-p"))
    stt = StreamingSTTSessionService(MockStreamingSTTProvider(final_after_frames=2))
    await stt.start(stream_sid=stream.stream_sid, params={"test_phrase": "Klinika manzili qayerda"})
    await stt.push_frame(_frame())
    await stt.push_frame(_frame())
    await stt.finish()
    await tsvc.attach_streaming_summary(stream, stt.summary(stopped_reason="stop_event"))
    await db_session.commit()

    r = await client.get(f"{API}/admin/telephony-streams/{stream.id}", headers=_bh(token))
    assert r.status_code == 200
    meta = r.json()["stream_metadata"]["streaming_stt"]
    assert meta["final_count"] == 1
    assert meta["final_transcripts"][0]["text"] == "Klinika manzili qayerda"
    assert meta["stopped_reason"] == "stop_event"
    assert "payload" not in r.text and "base64" not in r.text  # no raw audio anywhere


# --- WebSocket integration (dedicated engine + attach spy) ------------------
def _make_ws_client(monkeypatch, *, streaming: bool):
    import app.api.v1.telephony as tele

    monkeypatch.setattr(tele, "get_telephony_provider", lambda: _WS_PROVIDER)
    monkeypatch.setattr(settings, "twilio_use_media_streams", True)
    monkeypatch.setattr(settings, "streaming_stt_enabled", streaming)
    monkeypatch.setattr(settings, "streaming_stt_final_after_frames", 2)

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    state = {"init": False}

    async def _override():
        if not state["init"]:
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda c: Base.metadata.create_all(
                        c, tables=[Call.__table__, TelephonyCall.__table__, TelephonyStream.__table__]
                    )
                )
            state["init"] = True
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    return TestClient(app)


@pytest.fixture
def attach_spy(monkeypatch):
    """Capture the summary dict passed to attach_streaming_summary (no DB read)."""
    calls: list[dict] = []
    orig = TelephonyStreamService.attach_streaming_summary

    async def _spy(self, stream, summary):
        calls.append(summary)
        return await orig(self, stream, summary)

    monkeypatch.setattr(TelephonyStreamService, "attach_streaming_summary", _spy)
    return calls


def test_ws_streaming_disabled_attaches_no_summary(monkeypatch, attach_spy) -> None:
    c = _make_ws_client(monkeypatch, streaming=False)
    try:
        with c.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-off", "CA-off"))
            ws.send_json(_media(_b64(b"\x00" * 160)))
            ws.send_json({"event": "stop", "streamSid": "MZ-off", "stop": {}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()
    assert attach_spy == []  # streaming disabled -> no streaming_stt summary


def test_ws_streaming_attaches_final_summary_on_stop(monkeypatch, attach_spy) -> None:
    c = _make_ws_client(monkeypatch, streaming=True)
    try:
        with c.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-s", "CA-s", phrase="Klinika manzili qayerda"))
            ws.send_json(_media(_b64(b"\x00" * 160)))
            ws.send_json(_media(_b64(b"\x00" * 160), seq="3"))
            ws.send_json({"event": "stop", "streamSid": "MZ-s", "stop": {}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()
    assert len(attach_spy) >= 1
    s = attach_spy[-1]
    assert s["stopped_reason"] == "stop_event"
    assert s["final_count"] >= 1
    assert s["final_transcripts"][0]["text"] == "Klinika manzili qayerda"
    assert "payload" not in str(s) and "base64" not in str(s)


def test_ws_streaming_attaches_summary_on_disconnect(monkeypatch, attach_spy) -> None:
    c = _make_ws_client(monkeypatch, streaming=True)
    try:
        with c.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-d", "CA-d"))
            ws.send_json(_media(_b64(b"\x00" * 160)))
            ws.send_json(_media(_b64(b"\x00" * 160), seq="3"))
            # Let the server drain the queued start/media BEFORE the client
            # disconnect. Without this, a newer starlette TestClient can cancel the
            # server task on context exit before it reads the queue (server then
            # sees the disconnect with stream=None), making the finalize-on-
            # disconnect assertion flaky. Then exit without "stop" -> the server
            # finalizes via its `finally` (stopped_reason="disconnect").
            time.sleep(0.5)
    finally:
        app.dependency_overrides.clear()
    assert len(attach_spy) >= 1
    assert attach_spy[-1]["stopped_reason"] == "disconnect"


def test_ws_streaming_malformed_base64_does_not_crash(monkeypatch, attach_spy) -> None:
    c = _make_ws_client(monkeypatch, streaming=True)
    try:
        with c.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-b", "CA-b"))
            ws.send_json(_media("@@@not-base64@@@"))  # malformed -> empty payload, no crash
            ws.send_json(_media(_b64(b"\x00" * 160), seq="3"))
            ws.send_json({"event": "stop", "streamSid": "MZ-b", "stop": {}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()
    assert len(attach_spy) >= 1
    assert attach_spy[-1]["stopped_reason"] == "stop_event"


def test_ws_streaming_over_limit_closes_safely(monkeypatch, attach_spy) -> None:
    monkeypatch.setattr(settings, "streaming_stt_max_frames", 1)
    c = _make_ws_client(monkeypatch, streaming=True)
    try:
        with c.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-o", "CA-o"))
            ws.send_json(_media(_b64(b"\x00" * 160)))  # frame 1 ok
            ws.send_json(_media(_b64(b"\x00" * 160), seq="3"))  # frame 2 -> over limit
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()
    assert len(attach_spy) >= 1
    assert attach_spy[-1]["stopped_reason"] == "over_limit"
    assert attach_spy[-1]["over_limit"] is True


# --- A24.1 Finding A: duplicate start is superseded + session closed once ----
class _CloseCountingSession(StreamingSTTSession):
    def __init__(self, inner, counter) -> None:
        self._inner = inner
        self._counter = counter

    async def accept_audio_frame(self, frame):
        return await self._inner.accept_audio_frame(frame)

    async def finish_stream(self):
        return await self._inner.finish_stream()

    async def close(self):
        self._counter["n"] += 1
        return await self._inner.close()


def test_ws_duplicate_start_supersedes_and_closes(monkeypatch, attach_spy) -> None:
    import app.api.deps as depsmod

    close_calls = {"n": 0}

    class _Provider(StreamingSTTProvider):
        name = "mock"

        def __init__(self) -> None:
            self._m = MockStreamingSTTProvider(final_after_frames=2)

        def start_stream(self, context):
            return _CloseCountingSession(self._m.start_stream(context), close_calls)

    monkeypatch.setattr(depsmod, "get_streaming_stt_provider", lambda: _Provider())
    c = _make_ws_client(monkeypatch, streaming=True)
    try:
        with c.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-A", "CA-A"))
            ws.send_json(_ws_start("MZ-A2", "CA-A2"))  # duplicate start on same socket
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()
    # First stream finalized as superseded (no orphan active), session closed once,
    # and the second stream was never created.
    assert len(attach_spy) == 1
    assert attach_spy[-1]["stopped_reason"] == "superseded"
    assert close_calls["n"] == 1


# --- A24.1 Finding B: payload decoded once + oversized rejected --------------
def test_decode_media_payload_once_and_caps() -> None:
    from app.services.telephony.stream import decode_media_payload

    assert decode_media_payload(_b64(b"\x00" * 100), 8000) == b"\x00" * 100
    assert decode_media_payload(None, 8000) == b""
    assert decode_media_payload("@@@not-base64@@@", 8000) == b""
    # Oversized base64 is rejected by the length guard BEFORE decoding.
    assert decode_media_payload(_b64(b"\x00" * 100_000), 8000) == b""


def test_ws_streaming_decodes_payload_once_per_frame(monkeypatch, attach_spy) -> None:
    import app.api.v1.telephony as tele
    import app.services.telephony.stream as streammod

    counts = {"handler": 0, "service": 0}
    orig = streammod.decode_media_payload

    def _handler_decode(payload, m):
        counts["handler"] += 1
        return orig(payload, m)

    def _service_decode(payload, m):
        counts["service"] += 1
        return orig(payload, m)

    monkeypatch.setattr(tele, "decode_media_payload", _handler_decode)
    monkeypatch.setattr(streammod, "decode_media_payload", _service_decode)
    c = _make_ws_client(monkeypatch, streaming=True)
    try:
        with c.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-1d", "CA-1d"))
            ws.send_json(_media(_b64(b"\x00" * 160)))
            ws.send_json(_media(_b64(b"\x00" * 160), seq="3"))
            ws.send_json({"event": "stop", "streamSid": "MZ-1d", "stop": {}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()
    assert counts["handler"] == 2  # exactly one decode per media frame (handler)
    assert counts["service"] == 0  # record_media_frame reused decoded bytes (no 2nd decode)


def test_ws_streaming_oversized_frame_is_safe(monkeypatch, attach_spy) -> None:
    c = _make_ws_client(monkeypatch, streaming=True)
    huge = _b64(b"\x00" * 100_000)  # exceeds the pre-decode cap -> rejected, no crash
    try:
        with c.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-big", "CA-big"))
            ws.send_json(_media(huge))
            ws.send_json(_media(_b64(b"\x00" * 160), seq="3"))
            ws.send_json({"event": "stop", "streamSid": "MZ-big", "stop": {}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()
    assert len(attach_spy) >= 1
    assert attach_spy[-1]["stopped_reason"] == "stop_event"
    assert "payload" not in str(attach_spy[-1]) and "base64" not in str(attach_spy[-1])

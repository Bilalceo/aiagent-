"""Twilio Media Streams spike: TwiML toggle, WebSocket lifecycle, stream service.

No real Twilio, no paid providers, no streaming AI. WebSocket protocol behavior is
tested with Starlette's TestClient against a dedicated in-memory engine; DB-backed
counting is tested directly against the async session fixture.
"""
from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import deps
from app.core.db import Base, get_session
from app.main import app
from app.models.call import Call
from app.models.telephony_call import TelephonyCall
from app.models.telephony_stream import TelephonyStream
from app.services.auth.service import AuthService
from app.services.telephony.stream import TelephonyStreamService
from app.services.telephony.twilio import TwilioTelephonyProvider

API = "/api/v1"
VOICE = f"{API}/telephony/twilio/voice"
WS_URL = f"{API}/telephony/twilio/media-stream"
VALID = {"X-Twilio-Signature": "VALID"}

# Shared Twilio provider for WebSocket token signing/validation (no network).
_WS_PROVIDER = TwilioTelephonyProvider(
    auth_token="ws-secret", public_base_url="https://x", validate_signature=False
)
_OMIT = object()  # sentinel: omit the stream_token entirely


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# --- TwiML toggle (provider unit) -------------------------------------------
def test_media_stream_twiml_when_enabled() -> None:
    prov = TwilioTelephonyProvider(
        auth_token="t", public_base_url="https://x",
        use_media_streams=True, stream_url="wss://x/api/v1/telephony/twilio/media-stream",
    )
    xml = prov.build_media_stream_twiml(greeting="Salom", stream_url=prov.stream_url)
    assert xml.startswith("<?xml")
    assert "<Connect>" in xml and "<Stream" in xml
    assert 'url="wss://x/api/v1/telephony/twilio/media-stream"' in xml
    assert "<Gather" not in xml


def test_gather_twiml_when_media_streams_disabled() -> None:
    prov = TwilioTelephonyProvider(auth_token="t", public_base_url="https://x")
    assert prov.use_media_streams is False
    xml = prov.build_greeting_twiml(greeting="Salom", gather_action="https://x/g")
    assert "<Gather" in xml
    assert "<Connect>" not in xml


# --- endpoint: /twilio/voice honors the toggle ------------------------------
@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    async def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def media_stream_provider(monkeypatch):
    prov = TwilioTelephonyProvider(
        auth_token="t", public_base_url="https://example.test", validate_signature=True,
        use_media_streams=True, stream_url="wss://example.test/api/v1/telephony/twilio/media-stream",
        validator=lambda url, params, sig: sig == "VALID",
    )
    monkeypatch.setattr(deps, "get_telephony_provider", lambda: prov)
    return prov


@pytest.mark.asyncio
async def test_voice_returns_media_stream_twiml_when_enabled(client, media_stream_provider) -> None:
    r = await client.post(VOICE, data={"CallSid": "CA-ms", "From": "+1", "To": "+2"}, headers=VALID)
    assert r.status_code == 200
    xml = r.text
    assert "<Connect>" in xml and "<Stream" in xml
    assert "<Gather" not in xml
    # TwiML carries the signed stream token + call_sid as <Parameter>s.
    assert "stream_token" in xml and "<Parameter" in xml


# --- TelephonyStreamService (DB-backed counting) ----------------------------
def _start_event(stream_sid="MZ1", call_sid="CA1", token=_OMIT) -> dict:
    start = {
        "streamSid": stream_sid, "callSid": call_sid, "tracks": ["inbound"],
        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
    }
    # Default: a valid signed token. token=None omits it; a string injects it.
    tok = _WS_PROVIDER.make_stream_token(call_sid) if token is _OMIT else token
    if tok is not None:
        start["customParameters"] = {"call_sid": call_sid, "stream_token": tok}
    return {"event": "start", "sequenceNumber": "1", "streamSid": stream_sid, "start": start}


def _media_event(payload: str, seq="2") -> dict:
    return {"event": "media", "sequenceNumber": seq, "media": {"track": "inbound", "payload": payload}}


@pytest.mark.asyncio
async def test_stream_service_counts_frames_and_stops(db_session: AsyncSession) -> None:
    svc = TelephonyStreamService(db_session)
    stream = await svc.start_stream(_start_event())
    assert stream.id is not None
    assert stream.status == "active"
    assert stream.stream_metadata["media_format"]["encoding"] == "audio/x-mulaw"
    assert stream.stream_metadata["tracks"] == ["inbound"]

    n = await svc.record_media_frame(stream, _media_event(_b64(b"\x00" * 160)))
    assert n == 160
    assert stream.media_frames_count == 1
    assert stream.media_bytes_count == 160
    assert stream.last_sequence_number == 2

    # invalid base64 -> frame counted, zero bytes, no crash.
    n2 = await svc.record_media_frame(stream, _media_event("!!!not-base64!!!", seq="3"))
    assert n2 == 0
    assert stream.media_frames_count == 2

    stopped = await svc.stop_stream(stream)
    assert stopped.status == "stopped"
    assert stopped.stopped_at is not None


@pytest.mark.asyncio
async def test_stream_service_enforces_frame_and_byte_caps(db_session: AsyncSession) -> None:
    svc = TelephonyStreamService(db_session, max_frame_bytes=4, max_frames_per_call=2)
    stream = await svc.start_stream(_start_event("MZ2", "CA2"))
    payload = _b64(b"\x00" * 100)
    n = await svc.record_media_frame(stream, _media_event(payload))
    assert n == 4  # byte count capped
    await svc.record_media_frame(stream, _media_event(payload))
    await svc.record_media_frame(stream, _media_event(payload))  # beyond the cap
    assert stream.media_frames_count == 2  # frame count capped


@pytest.mark.asyncio
async def test_stream_links_existing_telephony_call(db_session: AsyncSession) -> None:
    tc = TelephonyCall(
        provider="twilio", provider_call_id="CA-link", status="in_progress", direction="inbound"
    )
    db_session.add(tc)
    await db_session.commit()
    svc = TelephonyStreamService(db_session)
    stream = await svc.start_stream(_start_event("MZ3", "CA-link"))
    assert stream.telephony_call_id == tc.id


# --- WebSocket endpoint (Starlette TestClient, dedicated engine) ------------
@pytest.fixture
def ws_client(monkeypatch):
    import app.api.v1.telephony as tele

    # WebSocket auth uses the Twilio provider's stream-token validator.
    monkeypatch.setattr(tele, "get_telephony_provider", lambda: _WS_PROVIDER)
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
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_ws_accepts_connected_start_media_stop(ws_client) -> None:
    with ws_client.websocket_connect(WS_URL) as ws:
        ws.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        ws.send_json(_start_event())
        ws.send_json(_media_event(_b64(b"\x00" * 160)))
        ws.send_json({"event": "stop", "sequenceNumber": "3", "streamSid": "MZ1", "stop": {"callSid": "CA1"}})
        # server closes the socket after stop
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_ws_malformed_json_closes_safely(ws_client) -> None:
    with ws_client.websocket_connect(WS_URL) as ws:
        ws.send_text("not-json{{{")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_ws_invalid_base64_media_is_safe(ws_client) -> None:
    with ws_client.websocket_connect(WS_URL) as ws:
        ws.send_json(_start_event("MZ9", "CA9"))
        ws.send_json(_media_event("@@@not-base64@@@"))  # must not crash the socket
        ws.send_json({"event": "stop", "streamSid": "MZ9", "stop": {}})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


# --- WebSocket auth (signed stream token) -----------------------------------
def test_stream_token_roundtrip_and_tampering() -> None:
    tok = _WS_PROVIDER.make_stream_token("CA-tok")
    assert _WS_PROVIDER.validate_stream_token(tok, call_sid="CA-tok")
    # wrong call_sid, tampered signature, and missing token all fail.
    assert not _WS_PROVIDER.validate_stream_token(tok, call_sid="CA-other")
    assert not _WS_PROVIDER.validate_stream_token(tok[:-1] + ("0" if tok[-1] != "0" else "1"))
    assert not _WS_PROVIDER.validate_stream_token(None)
    assert not _WS_PROVIDER.validate_stream_token("not.a.token")
    # expired token (exp in the past via now far in the future) fails.
    assert not _WS_PROVIDER.validate_stream_token(tok, now=9_999_999_999)


def test_ws_missing_token_rejected(ws_client) -> None:
    # No customParameters -> not authorized -> socket closed before any start_stream.
    with ws_client.websocket_connect(WS_URL) as ws:
        ws.send_json(_start_event("MZ-miss", "CA-miss", token=None))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_ws_invalid_token_rejected(ws_client) -> None:
    with ws_client.websocket_connect(WS_URL) as ws:
        ws.send_json(_start_event("MZ-bad", "CA-bad", token="CA-bad.9999999999.deadbeef"))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_ws_valid_token_accepted(ws_client) -> None:
    with ws_client.websocket_connect(WS_URL) as ws:
        ws.send_json(_start_event("MZ-ok", "CA-ok"))  # valid token by default
        ws.send_json(_media_event(_b64(b"\x00" * 160)))
        ws.send_json({"event": "stop", "streamSid": "MZ-ok", "stop": {}})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


# --- admin reads ------------------------------------------------------------
def _bh(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _user(db_session, email, role):
    await AuthService(db_session).create_user(
        email=email, password="rolepass001", full_name=role, role=role
    )
    await db_session.commit()


async def _token(client, email) -> str:
    r = await client.post(f"{API}/auth/login", json={"email": email, "password": "rolepass001"})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_admin_can_list_streams(client, db_session) -> None:
    await _user(db_session, "admin@clinic.uz", "admin")
    token = await _token(client, "admin@clinic.uz")
    await TelephonyStreamService(db_session).start_stream(_start_event("MZ-a", "CA-a"))
    await db_session.commit()

    r = await client.get(f"{API}/admin/telephony-streams", headers=_bh(token))
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["stream_sid"] == "MZ-a"
    assert "media_frames_count" in body[0]
    detail = await client.get(f"{API}/admin/telephony-streams/{body[0]['id']}", headers=_bh(token))
    assert detail.status_code == 200
    # The stream_token is never persisted/exposed in admin metadata.
    assert "stream_token" not in detail.text


@pytest.mark.asyncio
async def test_operator_cannot_list_streams(client, db_session) -> None:
    await _user(db_session, "op@clinic.uz", "operator")
    token = await _token(client, "op@clinic.uz")
    r = await client.get(f"{API}/admin/telephony-streams", headers=_bh(token))
    assert r.status_code == 403

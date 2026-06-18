"""Telephony intake spike: mock webhook flow + provider abstraction + admin read.

No real Twilio, no paid calls: the mock provider and a fake-free pipeline only.
"""
from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_telephony_provider
from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.services.auth.service import AuthService
from app.services.knowledge.seed import seed_demo_clinic
from app.services.telephony.mock import MockTelephonyProvider
from app.services.telephony.provider import TelephonyProvider
from app.services.telephony.service import TelephonyCallService
from app.services.voice.recordings import AudioRecordingService

API = "/api/v1"
MOCK_INBOUND = f"{API}/telephony/mock/inbound"


@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    async def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _b(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _user(db_session, email, role):
    await AuthService(db_session).create_user(
        email=email, password="rolepass001", full_name=role, role=role
    )
    await db_session.commit()


async def _token(client, email) -> str:
    r = await client.post(f"{API}/auth/login", json={"email": email, "password": "rolepass001"})
    return r.json()["access_token"]


# --- provider abstraction ---------------------------------------------------
def test_default_telephony_provider_is_mock() -> None:
    assert isinstance(get_telephony_provider(), MockTelephonyProvider)
    assert isinstance(get_telephony_provider(), TelephonyProvider)


def test_twilio_mode_missing_token_fails_fast(monkeypatch) -> None:
    monkeypatch.setattr(settings, "telephony_provider", "twilio")
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    with pytest.raises(RuntimeError):
        get_telephony_provider()


def test_validation_reason_does_not_leak_secret() -> None:
    provider = MockTelephonyProvider(webhook_secret="supersecret-value")
    res = provider.validate_inbound_request(headers={"x-telephony-secret": "wrong"}, body=b"{}")
    assert res.ok is False
    assert "supersecret-value" not in (res.reason or "")


# --- mock webhook flow ------------------------------------------------------
@pytest.mark.asyncio
async def test_mock_webhook_creates_telephony_call_and_session(client, db_session) -> None:
    r = await client.post(
        MOCK_INBOUND,
        json={"provider_call_id": "mock-1", "from_number": "+998901112233",
              "text_override": "Ish vaqtingiz qanday?"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "mock"
    assert body["call_session_id"] >= 1
    assert isinstance(body["ai_text"], str) and body["ai_text"]

    tels = await TelephonyCallService(db_session).list()
    assert len(tels) == 1
    assert tels[0].provider_call_id == "mock-1"
    assert tels[0].status == "processed"
    assert tels[0].call_session_id == body["call_session_id"]


@pytest.mark.asyncio
async def test_text_payload_returns_grounded_ai_text(client, db_session) -> None:
    await seed_demo_clinic(db_session)
    await db_session.commit()
    r = await client.post(MOCK_INBOUND, json={"text_override": "Klinika manzili qayerda?"})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "allow"
    assert body["ai_text"]
    assert body["audio"]["provider"] == "mock"
    assert "audio_bytes" not in body["audio"]  # never raw bytes


@pytest.mark.asyncio
async def test_audio_payload_creates_inbound_and_outbound_recordings(client, db_session) -> None:
    audio = base64.b64encode("Ish vaqtingiz qanday?".encode("utf-8")).decode()
    r = await client.post(MOCK_INBOUND, json={"audio_base64": audio, "content_type": "audio/wav"})
    assert r.status_code == 200
    call_id = r.json()["call_session_id"]

    recs = await AudioRecordingService(db_session).list_for_call(call_id)
    assert {rec.direction for rec in recs} == {"inbound", "outbound"}


@pytest.mark.asyncio
async def test_mock_webhook_twice_no_500(client) -> None:
    # Regression: repeated inbound with no from/to must not collide on the Call SID.
    for _ in range(2):
        r = await client.post(MOCK_INBOUND, json={"text_override": "Salom"})
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_voice_simulate_still_works(client) -> None:
    r = await client.post(f"{API}/voice/simulate", json={"text_override": "Nafas ololmayapman"})
    assert r.status_code == 200
    assert "103" in r.json()["ai_text"]


# --- error handling ---------------------------------------------------------
@pytest.mark.asyncio
async def test_invalid_secret_rejected(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "telephony_webhook_secret", "the-secret")
    r = await client.post(MOCK_INBOUND, json={"text_override": "Salom"})  # no secret header
    assert r.status_code == 403

    ok = await client.post(
        MOCK_INBOUND, json={"text_override": "Salom"}, headers={"X-Telephony-Secret": "the-secret"}
    )
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_oversized_payload_rejected(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "telephony_max_payload_bytes", 16)
    r = await client.post(MOCK_INBOUND, json={"text_override": "x" * 100})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_missing_text_and_audio_rejected(client) -> None:
    r = await client.post(MOCK_INBOUND, json={"provider_call_id": "mock-9"})
    assert r.status_code == 400  # provider parse error


# --- admin read RBAC --------------------------------------------------------
@pytest.mark.asyncio
async def test_admin_can_list_telephony_calls(client, db_session) -> None:
    await _user(db_session, "admin@clinic.uz", "admin")
    token = await _token(client, "admin@clinic.uz")
    await client.post(MOCK_INBOUND, json={"provider_call_id": "mock-a", "text_override": "Salom"})

    r = await client.get(f"{API}/admin/telephony-calls", headers=_b(token))
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["provider"] == "mock"
    # detail
    one = await client.get(f"{API}/admin/telephony-calls/{body[0]['id']}", headers=_b(token))
    assert one.status_code == 200
    assert one.json()["provider_call_id"] == "mock-a"


@pytest.mark.asyncio
async def test_operator_cannot_list_telephony_calls(client, db_session) -> None:
    await _user(db_session, "op@clinic.uz", "operator")
    token = await _token(client, "op@clinic.uz")
    r = await client.get(f"{API}/admin/telephony-calls", headers=_b(token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_admin_telephony_unauthenticated_401(client) -> None:
    r = await client.get(f"{API}/admin/telephony-calls")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_admin_telephony_filters_by_direction(client, db_session) -> None:
    await _user(db_session, "admin@clinic.uz", "admin")
    token = await _token(client, "admin@clinic.uz")
    await client.post(MOCK_INBOUND, json={"provider_call_id": "mock-d", "text_override": "Salom"})

    r = await client.get(
        f"{API}/admin/telephony-calls", params={"direction": "inbound"}, headers=_b(token)
    )
    assert r.status_code == 200
    assert all(rec["direction"] == "inbound" for rec in r.json())
    empty = await client.get(
        f"{API}/admin/telephony-calls", params={"direction": "outbound"}, headers=_b(token)
    )
    assert empty.json() == []


@pytest.mark.asyncio
async def test_admin_telephony_detail_redacts_sensitive_metadata(client, db_session) -> None:
    from app.models.telephony_call import TelephonyCall

    await _user(db_session, "admin@clinic.uz", "admin")
    token = await _token(client, "admin@clinic.uz")
    rec = TelephonyCall(
        provider="mock",
        provider_call_id="mock-x",
        status="processed",
        direction="inbound",
        raw_metadata={"from_number": "+998901112233", "webhook_secret": "should-not-leak"},
    )
    db_session.add(rec)
    await db_session.commit()

    r = await client.get(f"{API}/admin/telephony-calls/{rec.id}", headers=_b(token))
    assert r.status_code == 200
    meta = r.json()["raw_metadata"]
    assert meta["webhook_secret"] == "[REDACTED]"
    assert meta["from_number"] == "+998901112233"
    assert "should-not-leak" not in r.text

"""Voice provider readiness validation (A31) - config only, no network, no secrets.

Pure-service tests monkeypatch the global settings; an admin-endpoint test confirms
the route is manager-gated and leaks neither the Deepgram key nor the smoke token.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.services.auth.service import AuthService
from app.services.voice.readiness import VoiceProviderReadinessService, build_voice_readiness

API = "/api/v1"


def _svc() -> VoiceProviderReadinessService:
    return VoiceProviderReadinessService(settings)


def test_mock_providers_ready(monkeypatch) -> None:
    monkeypatch.setattr(settings, "streaming_stt_provider", "mock")
    monkeypatch.setattr(settings, "streaming_tts_provider", "mock")
    monkeypatch.setattr(settings, "live_call_smoke_mode", False)
    out = _svc().check()
    assert out["ready"] is True and out["errors"] == []
    assert out["summary"]["streaming_stt_provider"] == "mock"
    assert out["summary"]["deepgram_api_key_present"] is False


def test_deepgram_missing_key_not_ready_no_leak(monkeypatch) -> None:
    monkeypatch.setattr(settings, "streaming_stt_provider", "deepgram")
    monkeypatch.setattr(settings, "streaming_tts_provider", "deepgram")
    monkeypatch.setattr(settings, "deepgram_api_key", "")
    out = _svc().check()
    assert out["ready"] is False
    assert any("DEEPGRAM_API_KEY" in e for e in out["errors"])
    assert out["summary"]["deepgram_api_key_present"] is False


def test_deepgram_configured_ready_or_warnings_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, "streaming_stt_provider", "deepgram")
    monkeypatch.setattr(settings, "streaming_tts_provider", "deepgram")
    monkeypatch.setattr(settings, "deepgram_api_key", "dg-secret")
    monkeypatch.setattr(settings, "deepgram_encoding", "mulaw")
    monkeypatch.setattr(settings, "deepgram_sample_rate", 8000)
    monkeypatch.setattr(settings, "deepgram_tts_encoding", "mulaw")
    monkeypatch.setattr(settings, "deepgram_tts_sample_rate", 8000)
    monkeypatch.setattr(settings, "deepgram_tts_container", "none")
    out = _svc().check()
    assert out["ready"] is True and out["errors"] == []
    assert out["summary"]["stt_twilio_compatible"] is True
    assert out["summary"]["tts_twilio_compatible"] is True


def test_tts_wrong_container_not_ready(monkeypatch) -> None:
    monkeypatch.setattr(settings, "streaming_tts_provider", "deepgram")
    monkeypatch.setattr(settings, "deepgram_api_key", "dg-secret")
    monkeypatch.setattr(settings, "deepgram_tts_container", "wav")  # would add a header
    out = _svc().check()
    assert out["ready"] is False
    assert any("Twilio-compatible" in e for e in out["errors"])
    assert out["summary"]["tts_twilio_compatible"] is False


def test_smoke_mode_requires_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "live_call_smoke_mode", True)
    monkeypatch.setattr(settings, "live_call_require_smoke_token", True)
    monkeypatch.setattr(settings, "live_call_smoke_token", "")
    out = _svc().check()
    assert out["ready"] is False
    assert any("LIVE_CALL_SMOKE_TOKEN" in e for e in out["errors"])


def test_redact_transcripts_warns_metadata_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, "live_call_redact_transcripts", True)
    out = _svc().check()
    assert any(
        "Transcript redaction only applies to streaming metadata" in w
        and "Do not use real patient data" in w
        for w in out["warnings"]
    )
    # The warning carries no sensitive data.
    assert not any("token" in w.lower() and "smoke" in w.lower() for w in out["warnings"])


def test_readiness_output_has_no_secrets(monkeypatch) -> None:
    monkeypatch.setattr(settings, "streaming_stt_provider", "deepgram")
    monkeypatch.setattr(settings, "deepgram_api_key", "SUPERSECRETKEY123")
    monkeypatch.setattr(settings, "live_call_smoke_mode", True)
    monkeypatch.setattr(settings, "live_call_smoke_token", "SMOKETOKVAL456")
    out = _svc().check()
    blob = str(out)
    assert "SUPERSECRETKEY123" not in blob and "SMOKETOKVAL456" not in blob
    assert out["summary"]["deepgram_api_key_present"] is True
    assert out["summary"]["smoke_token_present"] is True


def test_build_voice_readiness_defaults_to_global() -> None:
    assert isinstance(build_voice_readiness(), VoiceProviderReadinessService)


# --- admin endpoint (manager-gated; no secret leak) -------------------------
@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    async def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await AuthService(db_session).create_user(
            email="root@clinic.uz", password="rootpw", full_name="Root", role="super_admin"
        )
        await db_session.commit()
        r = await c.post(f"{API}/auth/login", json={"email": "root@clinic.uz", "password": "rootpw"})
        c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_admin_readiness_endpoint_no_secret(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "deepgram_api_key", "ADMINSECRETKEY")
    monkeypatch.setattr(settings, "live_call_smoke_token", "ADMINSMOKETOK")
    r = await client.get(f"{API}/admin/voice-provider-readiness")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"ready", "warnings", "errors", "summary"}
    assert "ADMINSECRETKEY" not in r.text and "ADMINSMOKETOK" not in r.text


@pytest.mark.asyncio
async def test_admin_readiness_requires_auth() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(f"{API}/admin/voice-provider-readiness")
    assert r.status_code in (401, 403)

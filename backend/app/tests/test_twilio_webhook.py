"""Twilio Voice webhook: signature validation, form parsing, TwiML flow.

No real Twilio, no paid calls, no network. Signature checks use the deterministic
HMAC helper or an injected fake validator.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.api.deps import get_telephony_provider
from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.services.knowledge.seed import seed_demo_clinic
from app.services.telephony.service import TelephonyCallService
from app.services.telephony.twilio import (
    TwilioTelephonyProvider,
    compute_twilio_signature,
    validate_twilio_signature,
)
from app.services.voice.recordings import AudioRecordingService
from app.services.voice.tts import MockTTSProvider

API = "/api/v1"
VOICE = f"{API}/telephony/twilio/voice"
GATHER = f"{API}/telephony/twilio/gather"
VALID = {"X-Twilio-Signature": "VALID"}


# --- provider selection / fail-fast -----------------------------------------
def test_twilio_selected_only_when_env(monkeypatch) -> None:
    monkeypatch.setattr(settings, "telephony_provider", "twilio")
    monkeypatch.setattr(settings, "twilio_auth_token", "tok")
    monkeypatch.setattr(settings, "public_base_url", "https://example.test")
    assert isinstance(get_telephony_provider(), TwilioTelephonyProvider)


def test_twilio_missing_token_fails_fast(monkeypatch) -> None:
    monkeypatch.setattr(settings, "telephony_provider", "twilio")
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    with pytest.raises(RuntimeError):
        get_telephony_provider()


def test_twilio_signature_required_url_fails_fast(monkeypatch) -> None:
    monkeypatch.setattr(settings, "telephony_provider", "twilio")
    monkeypatch.setattr(settings, "twilio_auth_token", "tok")
    monkeypatch.setattr(settings, "twilio_validate_signature", True)
    monkeypatch.setattr(settings, "public_base_url", "")
    with pytest.raises(RuntimeError):
        get_telephony_provider()


# --- signature validation ---------------------------------------------------
def test_real_signature_roundtrip() -> None:
    token = "auth-token-abc"
    url = "https://example.test/api/v1/telephony/twilio/voice"
    params = {"CallSid": "CA1", "From": "+998901112233", "To": "+998711111111"}
    sig = compute_twilio_signature(token, url, params)
    assert validate_twilio_signature(token, url, params, sig)
    assert not validate_twilio_signature(token, url, params, "wrong-signature")
    assert not validate_twilio_signature("different-token", url, params, sig)


def test_authenticate_with_fake_validator() -> None:
    prov = TwilioTelephonyProvider(
        auth_token="t", public_base_url="https://x",
        validator=lambda url, params, sig: sig == "OK",
    )
    assert prov.authenticate(url="https://x/y", params={}, signature="OK").ok
    assert not prov.authenticate(url="https://x/y", params={}, signature="NO").ok
    assert not prov.authenticate(url="https://x/y", params={}, signature="").ok


def test_signature_validation_can_be_disabled() -> None:
    prov = TwilioTelephonyProvider(
        auth_token="t", public_base_url="https://x", validate_signature=False
    )
    assert prov.authenticate(url="https://x/y", params={}, signature="").ok


# --- form parsing + TwiML escaping ------------------------------------------
def test_parse_form_sanitizes_metadata() -> None:
    prov = TwilioTelephonyProvider(auth_token="t", public_base_url="https://x")
    ev = prov.parse_form({
        "CallSid": "CA1", "From": "+1", "To": "+2", "SpeechResult": "salom",
        "Confidence": "0.9", "AuthToken": "should-not-leak", "X-Twilio-Signature": "sig",
    })
    assert ev.provider_call_id == "CA1"
    assert ev.speech_result == "salom"
    assert ev.confidence == 0.9
    assert "AuthToken" not in ev.raw_metadata
    assert "X-Twilio-Signature" not in ev.raw_metadata
    assert "should-not-leak" not in str(ev.raw_metadata)


def test_parse_form_missing_callsid_raises() -> None:
    from app.services.telephony.provider import TelephonyParseError

    prov = TwilioTelephonyProvider(auth_token="t", public_base_url="https://x")
    with pytest.raises(TelephonyParseError):
        prov.parse_form({"From": "+1"})


def test_twiml_escapes_unsafe_text() -> None:
    prov = TwilioTelephonyProvider(auth_token="t", public_base_url="https://x")
    xml = prov.build_answer_twiml(
        ai_text='<bad>&"unsafe', gather_action="https://x/g", language="ru-RU"
    )
    assert xml.startswith("<?xml")
    assert "<bad>" not in xml
    assert "&lt;bad&gt;" in xml
    assert "<Response>" in xml and "<Gather" in xml


# --- endpoint flow ----------------------------------------------------------
@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    async def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def twilio_provider(monkeypatch):
    prov = TwilioTelephonyProvider(
        auth_token="test-token", public_base_url="https://example.test",
        validate_signature=True, validator=lambda url, params, sig: sig == "VALID",
    )
    monkeypatch.setattr(deps, "get_telephony_provider", lambda: prov)
    return prov


@pytest.mark.asyncio
async def test_twilio_voice_returns_twiml(client, twilio_provider) -> None:
    r = await client.post(
        VOICE,
        data={"CallSid": "CA-voice", "From": "+998901112233", "To": "+998711111111"},
        headers=VALID,
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    xml = r.text
    assert "<Response>" in xml and "<Gather" in xml
    assert 'action="https://example.test/api/v1/telephony/twilio/gather"' in xml


@pytest.mark.asyncio
async def test_twilio_invalid_signature_rejected(client, twilio_provider) -> None:
    r = await client.post(
        VOICE, data={"CallSid": "CA-bad", "From": "+1", "To": "+2"},
        headers={"X-Twilio-Signature": "BAD"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_twilio_gather_runs_pipeline(client, twilio_provider, db_session) -> None:
    await seed_demo_clinic(db_session)
    await db_session.commit()
    await client.post(
        VOICE, data={"CallSid": "CA-g", "From": "+998901112233", "To": "+998711111111"},
        headers=VALID,
    )
    r = await client.post(
        GATHER, data={"CallSid": "CA-g", "SpeechResult": "Klinika manzili qayerda?"},
        headers=VALID,
    )
    assert r.status_code == 200
    xml = r.text
    assert "<Say" in xml
    assert "<Gather" in xml  # normal answer re-gathers for the next question


@pytest.mark.asyncio
async def test_twilio_gather_missing_speech_returns_repeat(client, twilio_provider) -> None:
    await client.post(
        VOICE, data={"CallSid": "CA-r", "From": "+1", "To": "+2"}, headers=VALID
    )
    r = await client.post(GATHER, data={"CallSid": "CA-r"}, headers=VALID)
    assert r.status_code == 200
    assert "<Gather" in r.text  # asks the caller to repeat


@pytest.mark.asyncio
async def test_twilio_gather_transfer_returns_operator_twiml(client, twilio_provider) -> None:
    await client.post(
        VOICE, data={"CallSid": "CA-t", "From": "+1", "To": "+2"}, headers=VALID
    )
    r = await client.post(
        GATHER, data={"CallSid": "CA-t", "SpeechResult": "Qaysi dori ichsam bo'ladi?"},
        headers=VALID,
    )
    assert r.status_code == 200
    xml = r.text
    assert "<Hangup" in xml
    assert "<Gather" not in xml  # transfer ends the gather loop


@pytest.mark.asyncio
async def test_twilio_gather_emergency_returns_safe_twiml(client, twilio_provider) -> None:
    await client.post(
        VOICE, data={"CallSid": "CA-e", "From": "+1", "To": "+2"}, headers=VALID
    )
    r = await client.post(
        GATHER, data={"CallSid": "CA-e", "SpeechResult": "Nafas ololmayapman"},
        headers=VALID,
    )
    assert r.status_code == 200
    xml = r.text
    assert "103" in xml
    assert "<Gather" not in xml


@pytest.mark.asyncio
async def test_twilio_status_callback_updates_status(client, twilio_provider) -> None:
    await client.post(
        VOICE, data={"CallSid": "CA-s", "From": "+1", "To": "+2"}, headers=VALID
    )
    r = await client.post(
        f"{API}/telephony/twilio/status",
        data={"CallSid": "CA-s", "CallStatus": "completed"},
        headers=VALID,
    )
    assert r.status_code == 200
    assert "<Response" in r.text


# --- regression: idempotency + no-TTS-on-gather (A23.1) ---------------------
class _CountingTTS(MockTTSProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def synthesize(self, text, *, language, voice=None):
        self.calls += 1
        return await super().synthesize(text, language=language, voice=voice)


@pytest.mark.asyncio
async def test_twilio_voice_idempotent_on_same_callsid(client, twilio_provider, db_session) -> None:
    from sqlalchemy import func, select

    from app.models.call import Call
    from app.models.telephony_call import TelephonyCall

    data = {"CallSid": "CA-dup", "From": "+998901112233", "To": "+998711111111"}
    r1 = await client.post(VOICE, data=data, headers=VALID)
    r2 = await client.post(VOICE, data=data, headers=VALID)
    assert r1.status_code == 200 and r2.status_code == 200
    assert "<Response>" in r2.text and "<Gather" in r2.text

    calls = (
        await db_session.execute(
            select(func.count()).select_from(Call).where(Call.twilio_call_sid == "CA-dup")
        )
    ).scalar()
    tels = (
        await db_session.execute(
            select(func.count())
            .select_from(TelephonyCall)
            .where(TelephonyCall.provider_call_id == "CA-dup")
        )
    ).scalar()
    assert calls == 1  # idempotent: no duplicate Call
    assert tels == 1  # idempotent: no duplicate TelephonyCall


@pytest.mark.asyncio
async def test_twilio_gather_skips_tts_and_outbound_storage(
    client, twilio_provider, db_session, monkeypatch
) -> None:
    await seed_demo_clinic(db_session)
    await db_session.commit()
    tts = _CountingTTS()
    monkeypatch.setattr(deps, "get_tts_provider", lambda: tts)

    await client.post(
        VOICE, data={"CallSid": "CA-notts", "From": "+998901112233", "To": "+998711111111"},
        headers=VALID,
    )
    r = await client.post(
        GATHER, data={"CallSid": "CA-notts", "SpeechResult": "Klinika manzili qayerda?"},
        headers=VALID,
    )
    assert r.status_code == 200
    assert "<Say" in r.text  # ai_text spoken via TwiML
    assert tts.calls == 0  # Twilio speaks it; we never synthesize audio

    tel = (await TelephonyCallService(db_session).list(provider="twilio"))[0]
    recs = await AudioRecordingService(db_session).list_for_call(tel.call_session_id)
    assert all(rec.kind != "ai_tts" for rec in recs)  # no outbound audio stored

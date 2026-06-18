"""Voice layer: STT/TTS stubs + VoicePipelineService + /voice/simulate endpoint.

No real telephony/STT/TTS; deterministic mocks only.
"""
from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.main import app
from app.services.ai.provider import MockAIProvider
from app.services.ai.service import AIService
from app.services.audit.log import AuditLogService
from app.services.call.session import CallSessionService
from app.services.knowledge.seed import seed_demo_clinic
from app.services.knowledge.service import KBMatch, KnowledgeBaseService
from app.services.operator.availability import MockOperatorAvailability, OperatorState
from app.services.operator.transfer import OperatorTransferDecisionService
from app.services.voice.pipeline import VoicePipelineService
from app.services.auth.service import AuthService
from app.services.voice.recordings import AudioRecordingService
from app.services.voice.storage import (
    AudioStorageError,
    AudioStorageProvider,
    InMemoryAudioStorage,
)
from app.services.voice.stt import (
    MockSTTProvider,
    RealSTTProvider,
    STTProvider,
    STTProviderTimeoutError,
)
from app.services.voice.tts import (
    MockTTSProvider,
    RealTTSProvider,
    TTSProvider,
    TTSProviderTimeoutError,
)

API = "/api/v1"


class StubKnowledge:
    def __init__(self, chunks: list[str]) -> None:
        self._matches = [
            KBMatch(id=i + 1, title=f"item-{i + 1}", content=c, category="faq")
            for i, c in enumerate(chunks)
        ]

    async def search(self, query: str, language: str, intent=None) -> list[KBMatch]:
        return list(self._matches)


class _TimeoutSTT(STTProvider):
    async def transcribe(self, audio, *, content_type="application/octet-stream",
                         language=None, text_hint=None):
        raise STTProviderTimeoutError("stt timed out")


class _TimeoutTTS(TTSProvider):
    async def synthesize(self, text, *, language, voice=None):
        raise TTSProviderTimeoutError("tts timed out")


class _FakeOpenAITTS:
    """Minimal fake of openai client.audio.speech returning fixed bytes."""

    def __init__(self, content: bytes = b"ID3-FAKE-MP3") -> None:
        self._content = content
        self.audio = self

    @property
    def speech(self):
        return self

    async def create(self, **kwargs):
        class _Binary:
            pass

        b = _Binary()
        b.content = self._content
        return b


class _FailingStorage(AudioStorageProvider):
    provider = "failing"

    async def save_audio(self, data, *, content_type, duration_ms=None, metadata=None):
        raise AudioStorageError("storage down")

    async def get_signed_url(self, storage_key):
        raise AudioStorageError("storage down")

    async def delete_audio(self, storage_key):
        return None


def _pipeline(session, *, knowledge=None, chunks=None, stt=None, tts=None,
              operator_state=OperatorState.AVAILABLE, storage=None, recordings=None,
              with_storage=False) -> VoicePipelineService:
    kb = knowledge or StubKnowledge(chunks or [])
    ai = AIService(provider=MockAIProvider(), knowledge=kb)
    audit = AuditLogService(session)
    operator = OperatorTransferDecisionService(
        session, MockOperatorAvailability(operator_state), audit
    )
    css = CallSessionService(session, ai, audit, operator)
    if with_storage and storage is None:
        storage = InMemoryAudioStorage()
    if with_storage and recordings is None:
        recordings = AudioRecordingService(session)
    return VoicePipelineService(
        css, stt or MockSTTProvider(), tts or MockTTSProvider(),
        storage=storage, recordings=recordings,
    )


# --- provider stubs ---------------------------------------------------------
@pytest.mark.asyncio
async def test_mock_stt_returns_transcript_and_language() -> None:
    stt = MockSTTProvider()
    uz = await stt.transcribe(b"", text_hint="Klinika manzili qayerda?")
    assert uz.text == "Klinika manzili qayerda?"
    assert uz.language == "uz-UZ"
    assert uz.confidence > 0
    ru = await stt.transcribe("Сколько стоит приём?".encode("utf-8"))
    assert ru.text == "Сколько стоит приём?"
    assert ru.language == "ru-RU"


@pytest.mark.asyncio
async def test_mock_tts_returns_fake_audio() -> None:
    tts = MockTTSProvider()
    res = await tts.synthesize("Klinika 9:00-18:00 ishlaydi.", language="uz-UZ")
    assert res.audio_bytes is not None and res.audio_bytes.startswith(b"MOCK-AUDIO:")
    assert res.audio_url is None
    assert res.voice == "uz-UZ-MadinaNeural"
    ru = await tts.synthesize("Здравствуйте", language="ru-RU")
    assert ru.voice == "ru-RU-SvetlanaNeural"


# --- pipeline ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_pipeline_routes_transcript_through_ai(db_session: AsyncSession) -> None:
    pipe = _pipeline(db_session, chunks=["Klinika 9:00-18:00 ishlaydi."])
    out = await pipe.process(text_override="Ish vaqtingiz qanday?")
    assert out.transcript == "Ish vaqtingiz qanday?"
    assert out.ai_text == "Klinika 9:00-18:00 ishlaydi."
    assert out.action == "allow"
    assert out.audio is not None and out.audio.audio_bytes is not None


@pytest.mark.asyncio
async def test_unsafe_transcript_blocked_before_ai(db_session: AsyncSession) -> None:
    # StubKnowledge would echo a chunk if reached; the guard must transfer first.
    pipe = _pipeline(db_session, chunks=["LEAK should never appear"])
    out = await pipe.process(text_override="Qaysi dori ichsam bo'ladi?")
    assert out.transferred is True
    assert out.action == "transfer"
    assert "LEAK" not in out.ai_text


@pytest.mark.asyncio
async def test_emergency_returns_103_and_audio(db_session: AsyncSession) -> None:
    pipe = _pipeline(db_session, chunks=["irrelevant"])
    out = await pipe.process(text_override="Nafas ololmayapman!")
    assert out.action == "emergency"
    assert "103" in out.ai_text
    assert out.language == "uz-UZ"
    assert out.audio is not None  # TTS still voices the safe emergency message


@pytest.mark.asyncio
async def test_stt_timeout_maps_to_operator_transfer(db_session: AsyncSession) -> None:
    pipe = _pipeline(db_session, chunks=["x"], stt=_TimeoutSTT())
    out = await pipe.process(text_override="anything", language="uz-UZ")
    assert out.transferred is True
    assert out.action == "transfer"
    assert out.degraded_stage == "stt"
    assert out.audio is None


@pytest.mark.asyncio
async def test_language_preserved_into_tts(db_session: AsyncSession) -> None:
    pipe = _pipeline(db_session, chunks=["Приём с 9:00 до 18:00."])
    out = await pipe.process(text_override="Сколько стоит приём?")
    assert out.language == "ru-RU"
    assert out.audio is not None
    assert out.audio.language == "ru-RU"
    assert out.audio.voice == "ru-RU-SvetlanaNeural"


@pytest.mark.asyncio
async def test_sources_flow_through_voice_pipeline(db_session: AsyncSession) -> None:
    await seed_demo_clinic(db_session)
    await db_session.flush()
    pipe = _pipeline(db_session, knowledge=KnowledgeBaseService(db_session))
    out = await pipe.process(text_override="Klinika manzili qayerda?")
    assert out.action == "allow"
    assert out.sources  # KB grounding sources present in the voice outcome
    assert out.audio is not None


# --- endpoint ---------------------------------------------------------------
@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    async def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_voice_simulate_endpoint_text_override(client) -> None:
    resp = await client.post(f"{API}/voice/simulate", json={"text_override": "Nafas ololmayapman"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "emergency"
    assert "103" in body["ai_text"]
    assert body["audio"]["provider"] == "mock"
    assert body["stt"]["provider"] == "mock"


@pytest.mark.asyncio
async def test_voice_simulate_endpoint_audio_base64(client) -> None:
    audio = base64.b64encode("Ish vaqtingiz qanday?".encode("utf-8")).decode()
    resp = await client.post(f"{API}/voice/simulate", json={"audio_base64": audio})
    assert resp.status_code == 200
    body = resp.json()
    assert body["transcript"] == "Ish vaqtingiz qanday?"
    assert body["call_id"] >= 1


@pytest.mark.asyncio
async def test_voice_simulate_requires_input(client) -> None:
    resp = await client.post(f"{API}/voice/simulate", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_voice_simulate_handles_none_confidence(client, monkeypatch) -> None:
    # Regression: real STT returns confidence=None; the endpoint must not 500.
    from app.api import deps

    monkeypatch.setattr(deps, "get_stt_provider", lambda: RealSTTProvider(api_key="sk-test"))
    resp = await client.post(
        f"{API}/voice/simulate", json={"text_override": "Klinika manzili qayerda?"}
    )
    assert resp.status_code == 200
    assert resp.json()["stt"]["confidence"] is None


# --- audio storage / recordings --------------------------------------------
@pytest.mark.asyncio
async def test_inbound_and_outbound_recordings_created(db_session: AsyncSession) -> None:
    pipe = _pipeline(db_session, chunks=["Klinika 9:00-18:00 ishlaydi."], with_storage=True)
    out = await pipe.process(audio="Ish vaqtingiz qanday?".encode("utf-8"))
    assert out.inbound_recording_id is not None
    assert out.outbound_recording_id is not None

    recs = await AudioRecordingService(db_session).list_for_call(out.call_id)
    assert {r.direction for r in recs} == {"inbound", "outbound"}
    inbound = next(r for r in recs if r.direction == "inbound")
    outbound = next(r for r in recs if r.direction == "outbound")
    assert inbound.kind == "user_audio"
    assert inbound.transcript_text == "Ish vaqtingiz qanday?"
    assert len(inbound.checksum_sha256) == 64  # sha256 hex
    assert outbound.kind == "ai_tts"
    assert outbound.tts_voice == "uz-UZ-MadinaNeural"
    assert outbound.expires_at is not None


@pytest.mark.asyncio
async def test_text_override_skips_inbound_but_saves_outbound(db_session: AsyncSession) -> None:
    pipe = _pipeline(db_session, chunks=["x"], with_storage=True)
    out = await pipe.process(text_override="Ish vaqtingiz qanday?")
    assert out.inbound_recording_id is None  # no inbound audio bytes
    assert out.outbound_recording_id is not None


@pytest.mark.asyncio
async def test_storage_failure_maps_to_degraded(db_session: AsyncSession) -> None:
    pipe = _pipeline(db_session, chunks=["x"], with_storage=True, storage=_FailingStorage())
    out = await pipe.process(audio="Ish vaqtingiz qanday?".encode("utf-8"))
    assert out.transferred is True
    assert out.action == "transfer"
    assert out.degraded_stage == "storage"


@pytest.mark.asyncio
async def test_admin_can_list_recordings_for_call(client, db_session) -> None:
    await _user(db_session, "admin@clinic.uz", "admin")
    token = await _token(client, "admin@clinic.uz")
    pipe = _pipeline(db_session, chunks=["Klinika 9:00-18:00 ishlaydi."], with_storage=True)
    out = await pipe.process(audio="Ish vaqtingiz qanday?".encode("utf-8"))
    await db_session.commit()

    r = await client.get(
        f"{API}/admin/audio-recordings", params={"call_id": out.call_id}, headers=_b(token)
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    # no raw audio bytes anywhere in the response
    assert "audio_bytes" not in body[0]
    assert all("checksum_sha256" in rec for rec in body)


@pytest.mark.asyncio
async def test_admin_list_recordings_filters_by_direction_and_kind(client, db_session) -> None:
    await _user(db_session, "admin@clinic.uz", "admin")
    token = await _token(client, "admin@clinic.uz")
    pipe = _pipeline(db_session, chunks=["Klinika 9:00-18:00 ishlaydi."], with_storage=True)
    out = await pipe.process(audio="Ish vaqtingiz qanday?".encode("utf-8"))
    await db_session.commit()

    inbound = await client.get(
        f"{API}/admin/audio-recordings",
        params={"call_id": out.call_id, "direction": "inbound"},
        headers=_b(token),
    )
    assert inbound.status_code == 200
    body = inbound.json()
    assert len(body) == 1
    assert body[0]["direction"] == "inbound"
    assert body[0]["kind"] == "user_audio"

    outbound = await client.get(
        f"{API}/admin/audio-recordings",
        params={"call_id": out.call_id, "kind": "ai_tts"},
        headers=_b(token),
    )
    assert [r["direction"] for r in outbound.json()] == ["outbound"]


@pytest.mark.asyncio
async def test_admin_list_recordings_without_call_id_with_pagination(client, db_session) -> None:
    await _user(db_session, "admin@clinic.uz", "admin")
    token = await _token(client, "admin@clinic.uz")
    pipe = _pipeline(db_session, chunks=["x"], with_storage=True)
    await pipe.process(audio="Birinchi".encode("utf-8"), from_number="+998900000001")
    await pipe.process(audio="Ikkinchi".encode("utf-8"), from_number="+998900000002")
    await db_session.commit()

    # No call_id -> lists across calls; limit caps the page (newest first).
    page = await client.get(
        f"{API}/admin/audio-recordings", params={"limit": 1, "offset": 0}, headers=_b(token)
    )
    assert page.status_code == 200
    assert len(page.json()) == 1


@pytest.mark.asyncio
async def test_operator_cannot_list_recordings(client, db_session) -> None:
    await _user(db_session, "op@clinic.uz", "operator")
    token = await _token(client, "op@clinic.uz")
    r = await client.get(
        f"{API}/admin/audio-recordings", params={"call_id": 1}, headers=_b(token)
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_soft_delete_hides_recording(client, db_session) -> None:
    await _user(db_session, "admin@clinic.uz", "admin")
    token = await _token(client, "admin@clinic.uz")
    pipe = _pipeline(db_session, chunks=["x"], with_storage=True)
    out = await pipe.process(audio="Ish vaqtingiz qanday?".encode("utf-8"))
    await db_session.commit()
    recs = await AudioRecordingService(db_session).list_for_call(out.call_id)
    target = recs[0].id

    d = await client.post(f"{API}/admin/audio-recordings/{target}/delete", headers=_b(token))
    assert d.status_code == 200
    assert d.json()["is_deleted"] is True

    listed = await client.get(
        f"{API}/admin/audio-recordings", params={"call_id": out.call_id}, headers=_b(token)
    )
    ids = [rec["id"] for rec in listed.json()]
    assert target not in ids  # hidden from the default list


def _b(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _user(db_session, email, role):
    u = await AuthService(db_session).create_user(
        email=email, password="rolepass001", full_name=role, role=role
    )
    await db_session.commit()
    return u


async def _token(client, email) -> str:
    r = await client.post(f"{API}/auth/login", json={"email": email, "password": "rolepass001"})
    return r.json()["access_token"]


# --- real STT (fake client) flowing through the voice pipeline --------------
class _FakeWhisper:
    """Minimal fake of openai client.audio.transcriptions returning fixed text."""

    def __init__(self, text: str, language: str = "uzbek") -> None:
        self._text, self._lang = text, language
        self.audio = self

    @property
    def transcriptions(self):
        return self

    async def create(self, **kwargs):
        class _R:
            pass

        r = _R()
        r.text, r.language, r.duration = self._text, self._lang, 1.0
        return r


@pytest.mark.asyncio
async def test_real_stt_transcript_flows_and_is_stored(db_session: AsyncSession) -> None:
    stt = RealSTTProvider(api_key="sk-test", client=_FakeWhisper("Ish vaqtingiz qanday?"))
    pipe = _pipeline(db_session, chunks=["Klinika 9:00-18:00 ishlaydi."], stt=stt, with_storage=True)
    out = await pipe.process(audio=b"\x00\x01fake-wav", content_type="audio/wav")
    assert out.transcript == "Ish vaqtingiz qanday?"
    assert out.ai_text == "Klinika 9:00-18:00 ishlaydi."
    assert out.inbound_recording_id is not None

    recs = await AudioRecordingService(db_session).list_for_call(out.call_id)
    inbound = next(r for r in recs if r.direction == "inbound")
    assert inbound.transcript_text == "Ish vaqtingiz qanday?"
    assert inbound.content_type == "audio/wav"


@pytest.mark.asyncio
async def test_unsafe_real_stt_transcript_blocked_before_ai(db_session: AsyncSession) -> None:
    stt = RealSTTProvider(api_key="sk-test", client=_FakeWhisper("Qaysi dori ichsam bo'ladi?"))
    pipe = _pipeline(db_session, chunks=["LEAK should never appear"], stt=stt, with_storage=True)
    out = await pipe.process(audio=b"\x00fake", content_type="audio/wav")
    assert out.transferred is True
    assert out.action == "transfer"
    assert "LEAK" not in out.ai_text


# --- real TTS (fake client) flowing through the voice pipeline --------------
@pytest.mark.asyncio
async def test_real_tts_output_saved_as_outbound_recording(db_session: AsyncSession) -> None:
    tts = RealTTSProvider(
        api_key="sk-test", audio_format="mp3", client=_FakeOpenAITTS(b"ID3-FAKE-MP3")
    )
    pipe = _pipeline(
        db_session, chunks=["Klinika 9:00-18:00 ishlaydi."], tts=tts, with_storage=True
    )
    out = await pipe.process(text_override="Ish vaqtingiz qanday?")
    assert out.audio is not None
    assert out.audio.audio_bytes == b"ID3-FAKE-MP3"
    assert out.audio.content_type == "audio/mpeg"
    assert out.outbound_recording_id is not None

    recs = await AudioRecordingService(db_session).list_for_call(out.call_id)
    outbound = next(r for r in recs if r.direction == "outbound")
    assert outbound.content_type == "audio/mpeg"
    assert outbound.tts_text == "Klinika 9:00-18:00 ishlaydi."
    assert len(outbound.checksum_sha256) == 64
    assert outbound.size_bytes == len(b"ID3-FAKE-MP3")


@pytest.mark.asyncio
async def test_tts_timeout_maps_to_operator_transfer(db_session: AsyncSession) -> None:
    pipe = _pipeline(db_session, chunks=["Klinika 9:00-18:00 ishlaydi."], tts=_TimeoutTTS())
    out = await pipe.process(text_override="Ish vaqtingiz qanday?")
    assert out.transferred is True
    assert out.action == "transfer"
    assert out.degraded_stage == "tts"
    assert out.audio is None


@pytest.mark.asyncio
async def test_real_tts_endpoint_returns_no_raw_audio_bytes(client, db_session) -> None:
    # Wire the real TTS adapter (fake client) into the endpoint via deps override.
    from app.api import deps

    def _tts():
        return RealTTSProvider(api_key="sk-test", client=_FakeOpenAITTS())

    deps_orig = deps.get_tts_provider
    deps.get_tts_provider = _tts
    try:
        resp = await client.post(
            f"{API}/voice/simulate", json={"text_override": "Ish vaqtingiz qanday?"}
        )
    finally:
        deps.get_tts_provider = deps_orig
    assert resp.status_code == 200
    body = resp.json()
    assert body["audio"]["provider"] == "openai_tts"
    assert body["audio"]["content_type"] == "audio/mpeg"
    assert "audio_bytes" not in body["audio"]  # raw bytes never returned
    assert body["audio"]["audio_bytes_len"] > 0


# --- endpoint input validation ---------------------------------------------
@pytest.mark.asyncio
async def test_oversized_audio_rejected(client, monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "stt_max_audio_bytes", 8)
    big = base64.b64encode(b"x" * 64).decode()
    r = await client.post(f"{API}/voice/simulate", json={"audio_base64": big})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_unsupported_content_type_rejected(client) -> None:
    audio = base64.b64encode(b"abc").decode()
    r = await client.post(
        f"{API}/voice/simulate",
        json={"audio_base64": audio, "content_type": "application/x-evil"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_invalid_base64_rejected(client) -> None:
    r = await client.post(f"{API}/voice/simulate", json={"audio_base64": "!!!not-base64!!!"})
    assert r.status_code == 422

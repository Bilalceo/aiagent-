"""Common FastAPI dependencies and service wiring."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session  # noqa: F401  (re-exported for routers)
from app.core.security import require_api_key  # noqa: F401
from app.services.ai.provider import AIProvider, ClaudeAIProvider, MockAIProvider
from app.services.ai.service import AIService
from app.services.audit.log import AuditLogService
from app.services.call.session import CallSessionService
from app.services.knowledge.service import KnowledgeBaseService
from app.services.operator.availability import MockOperatorAvailability
from app.services.operator.transfer import OperatorTransferDecisionService
from app.services.telephony.mock import MockTelephonyProvider
from app.services.telephony.provider import TelephonyProvider
from app.services.telephony.service import TelephonyIntakeService, TwilioTelephonyService
from app.services.telephony.stream import TelephonyStreamService
from app.services.telephony.twilio import TwilioTelephonyProvider
from app.services.voice.pipeline import VoicePipelineService
from app.services.voice.recordings import AudioRecordingService
from app.services.voice.storage import (
    AudioStorageProvider,
    InMemoryAudioStorage,
    LocalAudioStorage,
)
from app.services.voice.streaming_stt import (
    MockStreamingSTTProvider,
    StreamingSTTProvider,
    StreamingSTTSessionService,
)
from app.services.voice.deepgram_stt import DeepgramStreamingSTTProvider
from app.services.voice.deepgram_tts import DeepgramStreamingTTSProvider
from app.services.voice.streaming_metrics import StreamingLatencyTracker
from app.services.voice.streaming_tts import (
    BargeInController,
    MockStreamingTTSProvider,
    StreamingTTSProvider,
    TwilioPlaybackService,
)
from app.services.voice.streaming_turn import StreamingTurnService
from app.services.voice.stt import MockSTTProvider, RealSTTProvider, STTProvider
from app.services.voice.tts import (
    TTS_FORMAT_CONTENT_TYPES,
    MockTTSProvider,
    RealTTSProvider,
    TTSProvider,
)


def get_ai_provider() -> AIProvider:
    """Select the AI provider from AI_PROVIDER (default mock). Fails fast if
    claude is requested without an API key."""
    if settings.ai_provider == "claude":
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "AI_PROVIDER=claude requires ANTHROPIC_API_KEY to be set"
            )
        return ClaudeAIProvider(
            model=settings.claude_model,
            api_key=settings.anthropic_api_key,
            timeout=settings.ai_timeout_seconds,
            max_tokens=settings.ai_max_tokens,
            temperature=settings.ai_temperature,
        )
    return MockAIProvider()


def build_call_session_service(session: AsyncSession) -> CallSessionService:
    knowledge = KnowledgeBaseService(session)
    ai_service = AIService(provider=get_ai_provider(), knowledge=knowledge)
    audit = AuditLogService(session)
    # MVP: operators are modelled as available; swap for a real presence provider later.
    operator = OperatorTransferDecisionService(
        session, MockOperatorAvailability(), audit
    )
    return CallSessionService(session, ai_service, audit, operator)


def get_stt_provider() -> STTProvider:
    """STT provider from STT_PROVIDER (default mock). openai_whisper is opt-in and
    fails fast without an API key."""
    if settings.stt_provider in ("openai_whisper", "whisper"):
        if not settings.openai_api_key:
            raise RuntimeError(
                "STT_PROVIDER=openai_whisper requires OPENAI_API_KEY to be set"
            )
        return RealSTTProvider(
            api_key=settings.openai_api_key,
            model=settings.stt_model,
            timeout=settings.stt_timeout_seconds,
            language_hint=settings.stt_language_hint or None,
        )
    if settings.stt_provider == "mock":
        return MockSTTProvider(timeout=settings.stt_timeout_seconds)
    raise RuntimeError(
        f"STT_PROVIDER={settings.stt_provider} is not implemented (mock|openai_whisper)"
    )


def get_tts_provider() -> TTSProvider:
    """TTS provider from TTS_PROVIDER (default mock). openai_tts is opt-in and
    fails fast without an API key or with an unsupported audio format."""
    if settings.tts_provider in ("openai_tts", "openai"):
        if not settings.openai_api_key:
            raise RuntimeError(
                "TTS_PROVIDER=openai_tts requires OPENAI_API_KEY to be set"
            )
        fmt = settings.tts_audio_format.lower()
        if fmt not in TTS_FORMAT_CONTENT_TYPES:
            raise RuntimeError(
                f"TTS_AUDIO_FORMAT={settings.tts_audio_format} is not supported "
                f"({'|'.join(TTS_FORMAT_CONTENT_TYPES)})"
            )
        return RealTTSProvider(
            api_key=settings.openai_api_key,
            model=settings.tts_model,
            voice_uz=settings.tts_voice_uz or settings.default_voice_uz,
            voice_ru=settings.tts_voice_ru or settings.default_voice_ru,
            audio_format=fmt,
            timeout=settings.tts_timeout_seconds,
            max_text_chars=settings.tts_max_text_chars,
        )
    if settings.tts_provider == "mock":
        return MockTTSProvider(
            voice_uz=settings.default_voice_uz,
            voice_ru=settings.default_voice_ru,
            timeout=settings.tts_timeout_seconds,
        )
    raise RuntimeError(
        f"TTS_PROVIDER={settings.tts_provider} is not implemented (mock|openai_tts)"
    )


def get_audio_storage() -> AudioStorageProvider:
    """Audio storage from AUDIO_STORAGE_PROVIDER (default memory). S3 not wired yet."""
    provider = settings.audio_storage_provider
    if provider == "memory":
        return InMemoryAudioStorage(signed_url_ttl_seconds=settings.signed_url_ttl_seconds)
    if provider == "local":
        return LocalAudioStorage(
            settings.audio_storage_path, signed_url_ttl_seconds=settings.signed_url_ttl_seconds
        )
    raise RuntimeError(
        f"AUDIO_STORAGE_PROVIDER={provider} is not implemented yet (memory|local only)"
    )


def build_audio_recording_service(session: AsyncSession) -> AudioRecordingService:
    return AudioRecordingService(session, retention_days=settings.audio_retention_days)


def build_voice_pipeline_service(session: AsyncSession) -> VoicePipelineService:
    return VoicePipelineService(
        build_call_session_service(session),
        get_stt_provider(),
        get_tts_provider(),
        storage=get_audio_storage(),
        recordings=build_audio_recording_service(session),
    )


def get_telephony_provider() -> TelephonyProvider:
    """Telephony provider from TELEPHONY_PROVIDER (default mock). twilio is a
    skeleton and fails fast without an auth token."""
    if settings.telephony_provider == "twilio":
        if not settings.twilio_auth_token:
            raise RuntimeError(
                "TELEPHONY_PROVIDER=twilio requires TWILIO_AUTH_TOKEN to be set"
            )
        if settings.twilio_validate_signature and not settings.public_base_url:
            raise RuntimeError(
                "Twilio signature validation requires PUBLIC_BASE_URL to be set"
            )
        return TwilioTelephonyProvider(
            auth_token=settings.twilio_auth_token,
            account_sid=settings.twilio_account_sid,
            public_base_url=settings.public_base_url,
            validate_signature=settings.twilio_validate_signature,
            voice=settings.twilio_voice,
            gather_language=settings.twilio_gather_language,
            gather_timeout=settings.twilio_gather_timeout_seconds,
            use_media_streams=settings.twilio_use_media_streams,
            stream_url=settings.twilio_stream_url,
        )
    if settings.telephony_provider == "mock":
        return MockTelephonyProvider(webhook_secret=settings.telephony_webhook_secret)
    raise RuntimeError(
        f"TELEPHONY_PROVIDER={settings.telephony_provider} is not implemented (mock|twilio)"
    )


def build_telephony_intake_service(
    session: AsyncSession, provider: TelephonyProvider | None = None
) -> TelephonyIntakeService:
    return TelephonyIntakeService(
        session,
        provider or get_telephony_provider(),
        build_voice_pipeline_service(session),
        AuditLogService(session),
    )


def build_twilio_telephony_service(
    session: AsyncSession, provider: TwilioTelephonyProvider | None = None
) -> TwilioTelephonyService:
    if provider is None:
        got = get_telephony_provider()
        if not isinstance(got, TwilioTelephonyProvider):
            raise RuntimeError("Twilio endpoints require TELEPHONY_PROVIDER=twilio")
        provider = got
    return TwilioTelephonyService(
        session,
        provider,
        build_voice_pipeline_service(session),
        build_call_session_service(session),
        AuditLogService(session),
    )


def build_telephony_stream_service(session: AsyncSession) -> TelephonyStreamService:
    return TelephonyStreamService(
        session,
        max_frame_bytes=settings.twilio_stream_max_frame_bytes,
        max_frames_per_call=settings.twilio_stream_max_frames_per_call,
    )


def get_streaming_stt_provider() -> StreamingSTTProvider:
    """Streaming STT provider from STREAMING_STT_PROVIDER (default mock). The
    deepgram adapter is opt-in and fails fast without an API key."""
    if settings.streaming_stt_provider == "mock":
        return MockStreamingSTTProvider(
            final_after_frames=settings.streaming_stt_final_after_frames
        )
    if settings.streaming_stt_provider == "deepgram":
        if not settings.deepgram_api_key:
            raise RuntimeError(
                "STREAMING_STT_PROVIDER=deepgram requires DEEPGRAM_API_KEY to be set"
            )
        return DeepgramStreamingSTTProvider(
            api_key=settings.deepgram_api_key,
            model=settings.deepgram_model,
            language=settings.deepgram_language,
            encoding=settings.deepgram_encoding,
            sample_rate=settings.deepgram_sample_rate,
            interim_results=settings.deepgram_interim_results,
            endpointing=settings.deepgram_endpointing,
            connect_timeout=settings.deepgram_connect_timeout_seconds,
            recv_timeout=settings.deepgram_receive_timeout_seconds,
            max_message_bytes=settings.deepgram_max_message_bytes,
            max_chars=settings.deepgram_max_transcript_chars,
        )
    raise RuntimeError(
        f"STREAMING_STT_PROVIDER={settings.streaming_stt_provider} is not implemented "
        "(mock|deepgram)"
    )


def build_streaming_stt_session_service() -> StreamingSTTSessionService:
    return StreamingSTTSessionService(
        get_streaming_stt_provider(),
        max_frames=settings.streaming_stt_max_frames,
        max_bytes=settings.streaming_stt_max_bytes,
    )


def build_streaming_turn_service(session: AsyncSession) -> StreamingTurnService:
    """AI-turn runner for a FINAL streaming transcript. Reuses the full call/safety
    pipeline (CallSessionService); produces a text turn only (no streaming TTS)."""
    return StreamingTurnService(
        build_call_session_service(session),
        max_transcript_chars=settings.streaming_stt_max_transcript_chars,
    )


def get_streaming_tts_provider() -> StreamingTTSProvider:
    """Streaming TTS provider from STREAMING_TTS_PROVIDER (default mock). The
    deepgram adapter is opt-in and fails fast without an API key."""
    if settings.streaming_tts_provider == "mock":
        return MockStreamingTTSProvider()
    if settings.streaming_tts_provider == "deepgram":
        if not settings.deepgram_api_key:
            raise RuntimeError(
                "STREAMING_TTS_PROVIDER=deepgram requires DEEPGRAM_API_KEY to be set"
            )
        return DeepgramStreamingTTSProvider(
            api_key=settings.deepgram_api_key,
            model=settings.deepgram_tts_model,
            encoding=settings.deepgram_tts_encoding,
            sample_rate=settings.deepgram_tts_sample_rate,
            container=settings.deepgram_tts_container,
            speed=settings.deepgram_tts_speed,
            connect_timeout=settings.deepgram_tts_connect_timeout_seconds,
            recv_timeout=settings.deepgram_tts_receive_timeout_seconds,
            max_message_bytes=settings.deepgram_tts_max_message_bytes,
            max_chars=settings.streaming_tts_max_text_chars,
        )
    raise RuntimeError(
        f"STREAMING_TTS_PROVIDER={settings.streaming_tts_provider} is not implemented "
        "(mock|deepgram)"
    )


def build_streaming_playback_service() -> TwilioPlaybackService:
    """Mock-first outbound playback over the Twilio Media Stream."""
    return TwilioPlaybackService(
        get_streaming_tts_provider(),
        chunk_size=settings.streaming_tts_chunk_bytes,
        max_text_chars=settings.streaming_tts_max_text_chars,
        max_chunks=settings.streaming_tts_max_chunks_per_turn,
        voice_uz=settings.streaming_tts_voice_uz,
        voice_ru=settings.streaming_tts_voice_ru,
    )


def build_barge_in_controller() -> BargeInController:
    """Barge-in/mark state machine for one media stream (sends Twilio `clear`)."""
    return BargeInController(
        enabled=settings.barge_in_enabled,
        on_partial=settings.barge_in_on_partial,
        on_final=settings.barge_in_on_final,
        min_chars=settings.barge_in_min_transcript_chars,
    )


def build_latency_tracker() -> StreamingLatencyTracker:
    """Per-stream latency metrics tracker (instrumentation only; numbers only)."""
    return StreamingLatencyTracker(
        enabled=settings.streaming_metrics_enabled,
        include_timestamps=settings.streaming_metrics_include_timestamps,
    )

"""Ilova sozlamalari — env'dan o'qiladi (pydantic-settings)."""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Umumiy
    app_env: str = "development"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8000"
    api_key: str = "change-me-admin-api-key"

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@postgres:5432/callcenter"
    database_url_sync: str = "postgresql+psycopg://postgres:postgres@postgres:5432/callcenter"

    # Redis / Celery
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/1"
    celery_result_backend: str = "redis://redis:6379/2"

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    # Telephony intake — mock default; twilio is a real (non-streaming) webhook.
    telephony_provider: str = "mock"  # mock | twilio
    telephony_webhook_secret: str = ""  # optional; if set, mock webhook requires it
    telephony_max_payload_bytes: int = 1_000_000  # 1 MB safe default

    # Twilio Voice webhook (non-streaming Gather/SpeechResult; NOT Media Streams).
    twilio_validate_signature: bool = True  # verify X-Twilio-Signature in twilio mode
    twilio_voice: str = "alice"  # Twilio <Say> voice
    twilio_gather_language: str = "ru-RU"  # Twilio <Gather>/<Say> language locale
    twilio_gather_timeout_seconds: int = 5

    # Twilio Media Streams (WebSocket spike) — parser/lifecycle only, no streaming AI.
    twilio_use_media_streams: bool = False  # if true, /twilio/voice returns <Connect><Stream>
    twilio_stream_url: str = ""  # wss:// URL Twilio connects the media stream to
    twilio_stream_max_frame_bytes: int = 8000  # cap counted bytes per media frame
    twilio_stream_max_frames_per_call: int = 50_000  # cap frames processed per stream

    # Streaming STT (mock-first; runs on the media stream when enabled). No AI/TTS.
    streaming_stt_provider: str = "mock"  # mock | deepgram (deepgram opt-in, real)
    streaming_stt_enabled: bool = False  # only meaningful with TWILIO_USE_MEDIA_STREAMS
    streaming_stt_max_frames: int = 10_000  # per-stream frame cap (then close safely)
    streaming_stt_max_bytes: int = 8_000_000  # per-stream byte cap
    streaming_stt_final_after_frames: int = 25  # mock: emit final after N frames

    # Real streaming STT (Deepgram) - only used when STREAMING_STT_PROVIDER=deepgram.
    # Tests NEVER call Deepgram (a fake connection is injected). The API key is never
    # logged or persisted. Encoding/sample-rate default to Twilio Media Streams audio.
    deepgram_api_key: str = ""  # required when provider=deepgram (fail fast if empty)
    deepgram_model: str = "nova-2"
    deepgram_language: str = ""  # optional (e.g. "ru", "multi"); empty -> provider default
    deepgram_encoding: str = "mulaw"  # Twilio media is 8k mu-law
    deepgram_sample_rate: int = 8000
    deepgram_interim_results: bool = True  # drive barge-in from interim transcripts
    deepgram_endpointing: str = ""  # optional ("false" or ms, e.g. "300"); empty -> default
    deepgram_connect_timeout_seconds: float = 5.0
    deepgram_receive_timeout_seconds: float = 0.05  # per-drain recv timeout (non-blocking)
    deepgram_max_message_bytes: int = 1_000_000  # cap a single inbound message
    deepgram_max_transcript_chars: int = 2000  # cap a parsed transcript

    # Streaming AI turns: route a FINAL streaming transcript through the existing
    # AI/safety pipeline (text-only; NO streaming TTS / no audio sent back yet).
    streaming_stt_ai_turns_enabled: bool = True  # final transcript -> one AI text turn
    streaming_stt_max_turns: int = 50  # cap AI turns per stream (bounds metadata growth)
    streaming_stt_max_transcript_chars: int = 2000  # cap transcript chars per turn

    # Streaming TTS playback (mock-first): when on, an AI text turn's reply is
    # synthesized (mock) and sent back as Twilio Media Streams `media` + `mark`
    # events over the SAME socket. No barge-in, no real provider yet.
    streaming_tts_enabled: bool = False  # default off -> AI turn persisted, no outbound media
    streaming_tts_provider: str = "mock"  # mock | deepgram (deepgram opt-in, real)
    streaming_tts_chunk_bytes: int = 400  # outbound audio bytes per media frame (pre-base64)
    streaming_tts_max_text_chars: int = 2000  # cap reply chars synthesized per turn
    streaming_tts_max_chunks_per_turn: int = 200  # cap media frames per turn (bounds output)
    streaming_tts_voice_uz: str = "uz-UZ-MadinaNeural"
    streaming_tts_voice_ru: str = "ru-RU-SvetlanaNeural"

    # Real streaming TTS (Deepgram) - only used when STREAMING_TTS_PROVIDER=deepgram.
    # Reuses DEEPGRAM_API_KEY. Tests NEVER call Deepgram (a fake connection is
    # injected). The key is never logged or persisted. encoding/sample_rate/container
    # default to RAW 8k mu-law so Twilio Media Streams can play frames directly.
    deepgram_tts_model: str = "aura-asteria-en"  # Deepgram TTS voice/model id
    deepgram_tts_encoding: str = "mulaw"  # Twilio media is 8k mu-law
    deepgram_tts_sample_rate: int = 8000
    deepgram_tts_container: str = "none"  # RAW frames, no WAV/RIFF header
    deepgram_tts_speed: str = ""  # optional (e.g. "1.1"); empty -> provider default
    deepgram_tts_connect_timeout_seconds: float = 5.0
    deepgram_tts_receive_timeout_seconds: float = 5.0  # wait for synthesized audio
    deepgram_tts_max_message_bytes: int = 1_000_000  # cap a single inbound message

    # Barge-in (mock-first): when the caller speaks (a streaming partial/final
    # transcript) while playback is active, send a Twilio `clear` to interrupt the
    # queued audio. No real VAD; the streaming STT transcript IS the speech signal.
    barge_in_enabled: bool = False  # default off -> keep A26 playback behavior
    barge_in_on_partial: bool = True  # a partial transcript triggers barge-in
    barge_in_on_final: bool = True  # a final transcript triggers barge-in
    barge_in_min_transcript_chars: int = 1  # ignore shorter (noise) transcripts

    # Streaming latency metrics (instrumentation only): record pipeline event
    # offsets + durations (ms) into stream_metadata.latency. No audio, no payloads.
    streaming_metrics_enabled: bool = True  # safe numeric metrics, on by default
    streaming_metrics_include_timestamps: bool = False  # add wall-clock ISO times (default off)

    # Live-call smoke mode (A31): a controlled real-call PILOT gate before clinic
    # usage. Default OFF -> nothing changes (no gating, no redaction). When ON it
    # gates the Twilio media-stream WebSocket (optional smoke token + caller
    # allowlist) and enforces max duration/turns. The smoke token is a shared secret
    # from env and is NEVER logged, persisted, or returned by the readiness endpoint.
    live_call_smoke_mode: bool = False  # gate the media stream as a controlled pilot
    live_call_max_duration_seconds: int = 180  # hard cap per smoke call (cost/safety)
    live_call_max_turns: int = 10  # hard cap on AI turns per smoke call
    live_call_allowed_caller_numbers: str = ""  # optional comma-separated allowlist
    live_call_require_smoke_token: bool = True  # require a valid smoke token in smoke mode
    live_call_smoke_token: str = ""  # shared secret (env); never logged/persisted
    live_call_redact_transcripts: bool = False  # redact caller transcript text in metadata
    live_call_no_patient_data_notice: bool = True  # surface a "no patient data" reminder

    @property
    def live_call_allowed_caller_numbers_list(self) -> list[str]:
        return [s.strip() for s in self.live_call_allowed_caller_numbers.split(",") if s.strip()]

    # Azure Speech
    azure_speech_key: str = ""
    azure_speech_region: str = "westeurope"
    azure_tts_voice_uz: str = "uz-UZ-MadinaNeural"
    azure_tts_voice_ru: str = "ru-RU-SvetlanaNeural"
    stt_default_languages: str = "uz-UZ,ru-RU"

    # AI provider
    ai_provider: str = "mock"  # mock | claude
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"
    ai_timeout_seconds: float = 15.0
    ai_max_tokens: int = 1024
    ai_temperature: Optional[float] = None
    llm_agent_model: str = "claude-sonnet-4-6"
    llm_intent_model: str = "claude-haiku-4-5"

    # Voice layer (STT/TTS) — mock default; real STT/TTS opt-in behind flags.
    stt_provider: str = "mock"  # mock | openai_whisper
    tts_provider: str = "mock"  # mock | openai_tts
    stt_timeout_seconds: float = 15.0
    tts_timeout_seconds: float = 15.0
    default_voice_uz: str = "uz-UZ-MadinaNeural"
    default_voice_ru: str = "ru-RU-SvetlanaNeural"

    # Real TTS (OpenAI TTS) — only used when TTS_PROVIDER=openai_tts.
    # Reuses OPENAI_API_KEY. Voices are OpenAI voice names (alloy, nova, ...);
    # if left empty they fall back to DEFAULT_VOICE_UZ / DEFAULT_VOICE_RU.
    tts_model: str = "tts-1"
    tts_voice_uz: str = "alloy"
    tts_voice_ru: str = "nova"
    tts_audio_format: str = "mp3"  # mp3 | wav | opus | aac | flac | pcm
    tts_max_text_chars: int = 4000

    # Real STT (OpenAI Whisper) — only used when STT_PROVIDER=openai_whisper.
    openai_api_key: str = ""
    stt_model: str = "whisper-1"
    stt_language_hint: str = ""  # optional ISO-639-1 hint, e.g. "uz" or "ru"
    stt_max_audio_bytes: int = 25_000_000  # 25 MB (Whisper upload limit)
    stt_allowed_content_types: str = (
        "audio/wav,audio/x-wav,audio/mpeg,audio/mp4,audio/m4a,audio/webm,"
        "audio/ogg,application/octet-stream"
    )

    @property
    def stt_allowed_content_types_list(self) -> list[str]:
        return [s.strip() for s in self.stt_allowed_content_types.split(",") if s.strip()]

    # Audio storage (metadata in DB, blobs out of DB) — mock by default.
    audio_storage_provider: str = "memory"  # memory | local | s3_placeholder
    audio_storage_path: str = "./data/audio"
    audio_retention_days: int = 90
    signed_url_ttl_seconds: int = 300

    # RAG / embeddings
    embedding_provider: str = "azure"
    embedding_model: str = "text-embedding-3-large"
    embedding_dim: int = 1536

    # Auth / JWT
    jwt_secret: str = "dev-insecure-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 720
    two_factor_ticket_expire_minutes: int = 5

    @property
    def stt_languages(self) -> list[str]:
        return [s.strip() for s in self.stt_default_languages.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

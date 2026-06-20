"""Voice provider readiness validation (A31) - CONFIG ONLY, no network calls.

Validates that the real live-voice pipeline (Twilio media streams + Deepgram STT +
Deepgram TTS) is configured coherently BEFORE a controlled smoke call. It never
contacts Twilio or Deepgram and never reveals the API key or the smoke token - the
summary only reports presence flags and safe config values.

Returns: {ready, warnings, errors, summary}. `ready` is True iff there are no
blocking errors.
"""
from __future__ import annotations

from typing import Optional


class VoiceProviderReadinessService:
    """Pure config validator. Reads the injected settings object at check() time so
    monkeypatched settings are reflected. No DB, no provider network calls."""

    def __init__(self, settings) -> None:
        self._s = settings

    def check(self) -> dict:
        s = self._s
        warnings: list[str] = []
        errors: list[str] = []

        stt_provider = s.streaming_stt_provider
        tts_provider = s.streaming_tts_provider
        deepgram_used = "deepgram" in (stt_provider, tts_provider)
        key_present = bool(s.deepgram_api_key)

        # STT / TTS Twilio audio compatibility (8k mu-law; TTS also container=none).
        stt_compatible = (
            str(s.deepgram_encoding).lower() == "mulaw" and int(s.deepgram_sample_rate) == 8000
        )
        tts_compatible = (
            str(s.deepgram_tts_encoding).lower() == "mulaw"
            and int(s.deepgram_tts_sample_rate) == 8000
            and str(s.deepgram_tts_container).lower() == "none"
        )

        # --- blocking errors ------------------------------------------------
        if deepgram_used and not key_present:
            errors.append("DEEPGRAM_API_KEY missing while a provider is set to deepgram")
        if stt_provider == "deepgram" and not stt_compatible:
            errors.append(
                "Deepgram STT encoding/sample_rate not Twilio-compatible "
                "(need mulaw/8000)"
            )
        if tts_provider == "deepgram" and not tts_compatible:
            errors.append(
                "Deepgram TTS encoding/sample_rate/container not Twilio-compatible "
                "(need mulaw/8000/none) - audio would not play"
            )
        if s.live_call_smoke_mode and s.live_call_require_smoke_token and not s.live_call_smoke_token:
            errors.append(
                "LIVE_CALL_SMOKE_MODE on with require_smoke_token but no "
                "LIVE_CALL_SMOKE_TOKEN set"
            )

        # --- non-blocking warnings ------------------------------------------
        if not s.twilio_use_media_streams:
            warnings.append("TWILIO_USE_MEDIA_STREAMS is off - no media-stream call")
        if not s.streaming_stt_enabled:
            warnings.append("STREAMING_STT_ENABLED is off - no transcription")
        if not s.streaming_stt_ai_turns_enabled:
            warnings.append("STREAMING_STT_AI_TURNS_ENABLED is off - no AI replies")
        if not s.streaming_tts_enabled:
            warnings.append("STREAMING_TTS_ENABLED is off - no outbound audio")
        if s.live_call_smoke_mode and stt_provider == "mock":
            warnings.append("smoke mode on but STREAMING_STT_PROVIDER=mock (not real STT)")
        if s.live_call_smoke_mode and tts_provider == "mock":
            warnings.append("smoke mode on but STREAMING_TTS_PROVIDER=mock (not real TTS)")
        if not s.barge_in_enabled:
            warnings.append("BARGE_IN_ENABLED is off - caller cannot interrupt playback")
        if not s.streaming_metrics_enabled:
            warnings.append("STREAMING_METRICS_ENABLED is off - no latency metrics")
        if s.live_call_smoke_mode and not s.live_call_allowed_caller_numbers_list:
            warnings.append("no LIVE_CALL_ALLOWED_CALLER_NUMBERS - any caller is allowed")
        if s.live_call_smoke_mode and not s.live_call_redact_transcripts:
            warnings.append("LIVE_CALL_REDACT_TRANSCRIPTS is off - transcripts stored in clear")
        if s.live_call_redact_transcripts:
            warnings.append(
                "Transcript redaction only applies to streaming metadata; call "
                "transcript rows may still contain recognized text. Do not use real "
                "patient data in smoke mode."
            )

        summary = {
            "twilio_media_streams_enabled": bool(s.twilio_use_media_streams),
            "streaming_stt_enabled": bool(s.streaming_stt_enabled),
            "streaming_tts_enabled": bool(s.streaming_tts_enabled),
            "ai_turns_enabled": bool(s.streaming_stt_ai_turns_enabled),
            "streaming_stt_provider": stt_provider,
            "streaming_tts_provider": tts_provider,
            "deepgram_api_key_present": key_present,  # presence only, NEVER the key
            "deepgram_stt": {
                "model": s.deepgram_model,
                "encoding": s.deepgram_encoding,
                "sample_rate": int(s.deepgram_sample_rate),
            },
            "deepgram_tts": {
                "model": s.deepgram_tts_model,
                "encoding": s.deepgram_tts_encoding,
                "sample_rate": int(s.deepgram_tts_sample_rate),
                "container": s.deepgram_tts_container,
            },
            "stt_twilio_compatible": stt_compatible,
            "tts_twilio_compatible": tts_compatible,
            "barge_in_enabled": bool(s.barge_in_enabled),
            "metrics_enabled": bool(s.streaming_metrics_enabled),
            "max_call_duration_seconds": int(s.live_call_max_duration_seconds),
            "max_turns": int(s.live_call_max_turns),
            "smoke_mode_enabled": bool(s.live_call_smoke_mode),
            "smoke_token_present": bool(s.live_call_smoke_token),  # presence only
            "require_smoke_token": bool(s.live_call_require_smoke_token),
            "allowed_caller_numbers_count": len(s.live_call_allowed_caller_numbers_list),
            "redact_transcripts": bool(s.live_call_redact_transcripts),
            "no_patient_data_notice": bool(s.live_call_no_patient_data_notice),
        }

        return {
            "ready": len(errors) == 0,
            "warnings": warnings,
            "errors": errors,
            "summary": summary,
        }


def build_voice_readiness(settings: Optional[object] = None) -> VoiceProviderReadinessService:
    if settings is None:
        from app.core.config import settings as global_settings

        settings = global_settings
    return VoiceProviderReadinessService(settings)

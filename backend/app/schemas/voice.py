"""Schemas for the local voice simulation bridge (NOT real telephony)."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class VoiceSimulateRequest(BaseModel):
    call_id: Optional[int] = None
    # Provide ONE of these. text_override is the deterministic local-testing path;
    # audio_base64 is a fake audio payload (base64 of bytes) for the audio path.
    text_override: Optional[str] = None
    audio_base64: Optional[str] = None
    content_type: Optional[str] = None  # validated against STT_ALLOWED_CONTENT_TYPES
    from_number: str = "+998900000000"
    to_number: str = "+998711111111"
    language: Optional[str] = None


class AudioMeta(BaseModel):
    voice: str
    language: str
    content_type: Optional[str] = None
    duration_ms: Optional[int] = None
    audio_bytes_len: int = 0
    audio_url: Optional[str] = None
    provider: Optional[str] = None


class STTMeta(BaseModel):
    language: str
    # None when the provider does not return a calibrated confidence (e.g. Whisper).
    confidence: Optional[float] = None
    duration_ms: Optional[int] = None
    provider: Optional[str] = None


class VoiceSimulateResponse(BaseModel):
    call_id: int
    transcript: str
    ai_text: str
    action: str
    reason_code: str
    transferred: bool
    language: str
    transfer_reason: Optional[str] = None
    priority: Optional[str] = None
    transfer_status: Optional[str] = None
    callback_required: bool = False
    sources: list = Field(default_factory=list)
    degraded_stage: Optional[str] = None
    stt: Optional[STTMeta] = None
    audio: Optional[AudioMeta] = None
    inbound_recording_id: Optional[int] = None
    outbound_recording_id: Optional[int] = None

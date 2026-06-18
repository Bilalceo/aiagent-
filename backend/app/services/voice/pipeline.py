"""VoicePipelineService — bridges audio I/O to the existing text pipeline.

audio bytes -> STT -> CallSessionService (full safety + AI + transfer) -> TTS.
This is NOT real telephony: it is a local bridge so the voice layer can be built
and tested before SIP/Twilio and audio streaming exist. The safety pipeline is
unchanged; the text path is the single source of truth for AI behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.services.call.session import CallSessionService
from app.services.voice.recordings import AudioRecordingService
from app.services.voice.storage import AudioStorageError, AudioStorageProvider
from app.services.voice.stt import STTProvider, STTProviderError, STTResult
from app.services.voice.tts import TTSProvider, TTSProviderError, TTSResult

_DEGRADED_UZ = "Kechirasiz, sizni yaxshi eshitolmadim. Sizni operatorga ulayman."
_DEGRADED_RU = "Извините, я вас плохо расслышал. Я соединю вас с оператором."


@dataclass
class VoiceOutcome:
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
    sources: list = field(default_factory=list)
    audio: Optional[TTSResult] = None
    stt: Optional[STTResult] = None
    degraded_stage: Optional[str] = None  # "stt" | "tts" | "storage" when a provider failed
    inbound_recording_id: Optional[int] = None
    outbound_recording_id: Optional[int] = None


class VoicePipelineService:
    def __init__(
        self,
        session_service: CallSessionService,
        stt: STTProvider,
        tts: TTSProvider,
        storage: Optional[AudioStorageProvider] = None,
        recordings: Optional[AudioRecordingService] = None,
    ) -> None:
        self._css = session_service
        self._stt = stt
        self._tts = tts
        self._storage = storage
        self._recordings = recordings

    async def process(
        self,
        *,
        call_id: Optional[int] = None,
        audio: bytes = b"",
        content_type: str = "application/octet-stream",
        text_override: Optional[str] = None,
        from_number: str = "+998900000000",
        to_number: str = "+998711111111",
        language: Optional[str] = None,
        synthesize_audio: bool = True,
    ) -> VoiceOutcome:
        # 1) Ensure a call session exists (so a degraded path still has a call_id).
        if call_id is None:
            start = await self._css.start_call(
                from_number=from_number, to_number=to_number, language_code=language
            )
            call_id = start.call.id

        # 2) Speech-to-text. Failure -> safe operator transfer (we could not hear).
        try:
            stt = await self._stt.transcribe(
                audio, content_type=content_type, language=language, text_hint=text_override
            )
        except STTProviderError:
            return self._degraded(call_id, language or "uz-UZ", stage="stt")

        # 2b) Persist inbound audio metadata (only when real audio bytes are present).
        inbound_id: Optional[int] = None
        if audio and self._can_store():
            try:
                stored = await self._storage.save_audio(
                    audio, content_type=content_type, duration_ms=stt.duration_ms,
                )
                rec = await self._recordings.create(
                    call_session_id=call_id, direction="inbound", kind="user_audio",
                    stored=stored, transcript_text=stt.text,
                    transcript_language=stt.language, transcript_confidence=stt.confidence,
                )
                inbound_id = rec.id
            except AudioStorageError:
                return self._degraded(call_id, language or stt.language, stage="storage", stt=stt)

        # 3) Existing text pipeline (full safety + AI + transfer engine).
        outcome = await self._css.handle_message(
            call_id=call_id, text=stt.text, language=language or stt.language
        )

        # 4) Text-to-speech of the (already safety-checked) reply. Skipped when the
        # caller speaks the reply itself (e.g. Twilio <Say>), which avoids a wasted
        # (paid) synthesis + storage write per turn. Failure -> safe transfer.
        audio_result: Optional[TTSResult] = None
        outbound_id: Optional[int] = None
        if synthesize_audio:
            try:
                audio_result = await self._tts.synthesize(outcome.reply, language=outcome.language)
            except TTSProviderError:
                return self._degraded(
                    call_id, outcome.language, stage="tts", transcript=stt.text, stt=stt,
                    inbound_recording_id=inbound_id,
                )

            # 4b) Persist outbound TTS audio metadata.
            if audio_result.audio_bytes and self._can_store():
                try:
                    stored = await self._storage.save_audio(
                        audio_result.audio_bytes, content_type=audio_result.content_type,
                        duration_ms=audio_result.duration_ms,
                    )
                    rec = await self._recordings.create(
                        call_session_id=call_id, direction="outbound", kind="ai_tts",
                        stored=stored, tts_voice=audio_result.voice, tts_text=audio_result.text,
                    )
                    outbound_id = rec.id
                except AudioStorageError:
                    return self._degraded(
                        call_id, outcome.language, stage="storage", transcript=stt.text, stt=stt,
                        inbound_recording_id=inbound_id,
                    )

        return VoiceOutcome(
            call_id=call_id,
            transcript=stt.text,
            ai_text=outcome.reply,
            action=outcome.action,
            reason_code=outcome.reason_code,
            transferred=outcome.transferred,
            language=outcome.language,
            transfer_reason=outcome.transfer_reason,
            priority=outcome.priority,
            transfer_status=outcome.transfer_status,
            callback_required=outcome.callback_required,
            sources=outcome.sources,
            audio=audio_result,
            stt=stt,
            inbound_recording_id=inbound_id,
            outbound_recording_id=outbound_id,
        )

    def _can_store(self) -> bool:
        return self._storage is not None and self._recordings is not None

    @staticmethod
    def _degraded(
        call_id: int,
        language: str,
        *,
        stage: str,
        transcript: str = "",
        stt: Optional[STTResult] = None,
        inbound_recording_id: Optional[int] = None,
    ) -> VoiceOutcome:
        ru = language.startswith("ru")
        return VoiceOutcome(
            call_id=call_id,
            transcript=transcript,
            ai_text=_DEGRADED_RU if ru else _DEGRADED_UZ,
            action="transfer",
            reason_code="low_ai_confidence",
            transferred=True,
            language=language,
            transfer_reason="low_confidence",
            sources=[],
            audio=None,
            stt=stt,
            degraded_stage=stage,
            inbound_recording_id=inbound_recording_id,
        )

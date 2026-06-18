"""Telephony intake orchestration + read queries.

TelephonyIntakeService runs the webhook flow (validate -> parse -> persist ->
VoicePipelineService -> respond). Route handlers stay thin and only map errors to
HTTP status codes. TelephonyCallService backs the admin read endpoints.
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.telephony_call import TelephonyCall
from app.services.audit.log import AuditEvent, AuditLogService
from app.services.call.session import CallSessionService
from app.services.telephony.provider import (
    TelephonyParseError,
    TelephonyProvider,
    TelephonySignatureError,
    VoiceResponse,
)
from app.services.telephony.twilio import TwilioTelephonyProvider
from app.services.voice.pipeline import VoicePipelineService

_DEFAULT_FROM = "+998900000000"
_DEFAULT_TO = "+998711111111"

# Public paths Twilio posts to (used to reconstruct the signed URL + Gather action).
TWILIO_VOICE_PATH = "/api/v1/telephony/twilio/voice"
TWILIO_GATHER_PATH = "/api/v1/telephony/twilio/gather"


class TelephonyIntakeService:
    def __init__(
        self,
        session: AsyncSession,
        provider: TelephonyProvider,
        pipeline: VoicePipelineService,
        audit: AuditLogService,
    ) -> None:
        self._session = session
        self._provider = provider
        self._pipeline = pipeline
        self._audit = audit

    async def handle_inbound(self, *, headers: dict, body: bytes) -> VoiceResponse:
        validation = self._provider.validate_inbound_request(headers=headers, body=body)
        if not validation.ok:
            # reason is a safe, non-sensitive string (no secret echoed).
            raise TelephonySignatureError(validation.reason or "invalid signature")

        event = self._provider.parse_inbound_call(headers=headers, body=body)

        tel = TelephonyCall(
            provider=event.provider,
            provider_call_id=event.provider_call_id,
            from_number=event.from_number,
            to_number=event.to_number,
            status="received",
            direction="inbound",
            raw_metadata=event.raw_metadata or None,
        )
        self._session.add(tel)
        await self._session.flush()
        await self._audit.record(
            AuditEvent.TELEPHONY_CALL_STARTED,
            data={
                "telephony_call_id": tel.id,
                "provider": event.provider,
                "provider_call_id": event.provider_call_id,
            },
        )
        await self._session.commit()

        audio = b""
        if event.audio_base64:
            try:
                audio = base64.b64decode(event.audio_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                await self._fail(tel)
                raise TelephonyParseError("audio_base64 is not valid base64") from exc

        try:
            outcome = await self._pipeline.process(
                call_id=event.call_session_id,
                audio=audio,
                content_type=event.content_type or "application/octet-stream",
                text_override=event.text_override,
                from_number=event.from_number or _DEFAULT_FROM,
                to_number=event.to_number or _DEFAULT_TO,
                language=event.language,
            )
        except ValueError as exc:  # call_session_id not found
            await self._fail(tel)
            raise TelephonyParseError("call_session_id not found") from exc

        tel.call_session_id = outcome.call_id
        tel.status = "processed"
        tel.ended_at = datetime.now(timezone.utc)
        tel.raw_metadata = {
            **(tel.raw_metadata or {}),
            "action": outcome.action,
            "reason_code": outcome.reason_code,
            "transferred": outcome.transferred,
            "degraded_stage": outcome.degraded_stage,
        }
        await self._audit.record(
            AuditEvent.TELEPHONY_CALL_PROCESSED,
            call_id=outcome.call_id,
            data={
                "telephony_call_id": tel.id,
                "action": outcome.action,
                "transferred": outcome.transferred,
                "inbound_recording_id": outcome.inbound_recording_id,
                "outbound_recording_id": outcome.outbound_recording_id,
            },
        )
        await self._session.commit()
        return self._provider.build_voice_response(outcome)

    async def _fail(self, tel: TelephonyCall) -> None:
        tel.status = "failed"
        tel.ended_at = datetime.now(timezone.utc)
        await self._session.commit()


class TwilioTelephonyService:
    """Real Twilio Voice webhook flow (non-streaming Gather/SpeechResult).

    /voice: validate signature -> start CallSession -> greet + Gather.
    /gather: validate signature -> run pipeline on SpeechResult -> answer + Gather
             (or a safe operator/emergency message). Returns TwiML XML strings.
    """

    def __init__(
        self,
        session: AsyncSession,
        provider: TwilioTelephonyProvider,
        pipeline: VoicePipelineService,
        css: CallSessionService,
        audit: AuditLogService,
    ) -> None:
        self._session = session
        self._provider = provider
        self._pipeline = pipeline
        self._css = css
        self._audit = audit

    def _authenticate(self, *, path: str, form: dict, signature: str) -> None:
        url = self._provider.public_url_for(path)
        result = self._provider.authenticate(url=url, params=form, signature=signature)
        if not result.ok:
            raise TelephonySignatureError(result.reason or "invalid signature")

    async def handle_voice(self, *, form: dict, signature: str) -> str:
        self._authenticate(path=TWILIO_VOICE_PATH, form=form, signature=signature)
        event = self._provider.parse_form(form)

        # start_call is idempotent on CallSid, so a Twilio webhook retry reuses the
        # existing CallSession instead of inserting a duplicate.
        start = await self._css.start_call(
            from_number=event.from_number or _DEFAULT_FROM,
            to_number=event.to_number or _DEFAULT_TO,
            call_sid=event.provider_call_id,
        )
        tel = await self._find_by_sid(event.provider_call_id)
        if tel is None:
            tel = TelephonyCall(
                provider=self._provider.name,
                provider_call_id=event.provider_call_id,
                call_session_id=start.call.id,
                from_number=event.from_number,
                to_number=event.to_number,
                status="in_progress",
                direction=event.direction or "inbound",
                raw_metadata=event.raw_metadata or None,
            )
            self._session.add(tel)
            await self._session.flush()
            await self._audit.record(
                AuditEvent.TELEPHONY_CALL_STARTED,
                call_id=start.call.id,
                data={
                    "telephony_call_id": tel.id,
                    "provider": self._provider.name,
                    "provider_call_id": event.provider_call_id,
                },
            )
        else:
            # Idempotent retry: reuse the existing record, keep linkage/status.
            tel.call_session_id = start.call.id
            tel.status = "in_progress"
        await self._session.commit()

        # Media Streams (spike): connect to the WebSocket instead of Gather.
        if self._provider.use_media_streams and self._provider.stream_url:
            return self._provider.build_media_stream_twiml(
                greeting=start.greeting,
                stream_url=self._provider.stream_url,
                call_sid=event.provider_call_id or "",
            )
        gather_action = self._provider.public_url_for(TWILIO_GATHER_PATH)
        return self._provider.build_greeting_twiml(
            greeting=start.greeting, gather_action=gather_action
        )

    async def handle_gather(self, *, form: dict, signature: str) -> str:
        self._authenticate(path=TWILIO_GATHER_PATH, form=form, signature=signature)
        event = self._provider.parse_form(form)
        gather_action = self._provider.public_url_for(TWILIO_GATHER_PATH)

        tel = await self._find_by_sid(event.provider_call_id)
        if tel is None or tel.call_session_id is None:
            return self._provider.build_operator_twiml(
                message="Kechirasiz, qo'ng'iroq topilmadi. Iltimos qayta qo'ng'iroq qiling."
            )
        if not event.speech_result:
            return self._provider.build_repeat_twiml(gather_action=gather_action)

        try:
            # Twilio speaks ai_text via <Say>, so we skip TTS synthesis + outbound
            # audio storage (avoids a wasted paid synthesis + DB write per turn).
            outcome = await self._pipeline.process(
                call_id=tel.call_session_id,
                text_override=event.speech_result,
                from_number=tel.from_number or _DEFAULT_FROM,
                to_number=tel.to_number or _DEFAULT_TO,
                synthesize_audio=False,
            )
        except Exception:  # never leak a traceback into TwiML
            tel.status = "failed"
            await self._session.commit()
            return self._provider.build_error_twiml()

        tel.status = "processed"
        tel.raw_metadata = {
            **(tel.raw_metadata or {}),
            "action": outcome.action,
            "reason_code": outcome.reason_code,
            "transferred": outcome.transferred,
        }
        await self._audit.record(
            AuditEvent.TELEPHONY_CALL_PROCESSED,
            call_id=outcome.call_id,
            data={
                "telephony_call_id": tel.id,
                "action": outcome.action,
                "transferred": outcome.transferred,
            },
        )
        await self._session.commit()

        if outcome.transferred or outcome.action in ("transfer", "emergency"):
            return self._provider.build_operator_twiml(
                message=outcome.ai_text, language=outcome.language
            )
        return self._provider.build_answer_twiml(
            ai_text=outcome.ai_text, gather_action=gather_action, language=outcome.language
        )

    async def handle_status(self, *, form: dict, signature: str) -> str:
        self._authenticate(path="/api/v1/telephony/twilio/status", form=form, signature=signature)
        event = self._provider.parse_form(form)
        tel = await self._find_by_sid(event.provider_call_id)
        if tel is not None and event.call_status:
            if event.call_status.lower() in ("completed", "failed", "busy", "no-answer", "canceled"):
                tel.status = "completed" if event.call_status.lower() == "completed" else "failed"
                tel.ended_at = datetime.now(timezone.utc)
                await self._session.commit()
        # Twilio status callbacks expect a 2xx; an empty Response is valid TwiML.
        return '<?xml version="1.0" encoding="UTF-8"?><Response/>'

    async def _find_by_sid(self, sid: Optional[str]) -> Optional[TelephonyCall]:
        if not sid:
            return None
        stmt = (
            select(TelephonyCall)
            .where(TelephonyCall.provider_call_id == sid)
            .order_by(TelephonyCall.id.desc())
        )
        return (await self._session.execute(stmt)).scalars().first()


class TelephonyCallService:
    """Read-only queries for the admin telephony endpoints."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(
        self,
        *,
        provider: Optional[str] = None,
        status: Optional[str] = None,
        direction: Optional[str] = None,
        call_session_id: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TelephonyCall]:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        stmt = select(TelephonyCall)
        if provider:
            stmt = stmt.where(TelephonyCall.provider == provider)
        if status:
            stmt = stmt.where(TelephonyCall.status == status)
        if direction:
            stmt = stmt.where(TelephonyCall.direction == direction)
        if call_session_id is not None:
            stmt = stmt.where(TelephonyCall.call_session_id == call_session_id)
        stmt = stmt.order_by(TelephonyCall.id.desc()).limit(limit).offset(offset)
        return list((await self._session.execute(stmt)).scalars().all())

    async def get(self, telephony_call_id: int) -> Optional[TelephonyCall]:
        stmt = select(TelephonyCall).where(TelephonyCall.id == telephony_call_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

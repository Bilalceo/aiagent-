"""CallSessionService — call/session lifecycle for the text simulation.

Orchestrates greeting, language detection, AIService, transcripts, audit and the
operator transfer decision engine. No business logic lives in route handlers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.call import Call
from app.models.transcript import Transcript
from app.services.ai.provider import AIMessage
from app.services.ai.service import AIResult, AIService
from app.services.audit.log import AuditEvent, AuditLogService
from app.services.greeting import GreetingService
from app.services.language import Language, LanguageDetectionService
from app.services.operator.transfer import (
    OperatorTransferDecisionService,
    map_reason_code,
)
from app.services.safety.guard import SafetyAction


@dataclass
class CallStart:
    call: Call
    greeting: str
    language: Language


@dataclass
class MessageOutcome:
    reply: str
    action: str
    reason_code: str
    transferred: bool
    language: str
    transfer_reason: Optional[str] = None
    priority: Optional[str] = None
    transfer_status: Optional[str] = None
    callback_required: bool = False
    sources: list = field(default_factory=list)  # KB sources used: [{id, title}]


class CallSessionService:
    def __init__(
        self,
        session: AsyncSession,
        ai_service: AIService,
        audit: AuditLogService,
        operator: OperatorTransferDecisionService,
        language_detector: LanguageDetectionService | None = None,
        greeting: GreetingService | None = None,
    ) -> None:
        self._session = session
        self._ai = ai_service
        self._audit = audit
        self._operator = operator
        self._lang = language_detector or LanguageDetectionService()
        self._greeting = greeting or GreetingService()

    async def start_call(
        self,
        *,
        from_number: str,
        to_number: str,
        call_sid: str | None = None,
        language_code: str | None = None,
    ) -> CallStart:
        lang = Language.from_code(language_code) or Language.UNKNOWN
        greeting = self._greeting.greet(lang)

        # Idempotency: a telephony provider may RETRY the inbound webhook with the
        # same call_sid. Reuse the existing call instead of inserting a duplicate
        # (calls.twilio_call_sid is UNIQUE). When no call_sid is supplied, generate
        # a unique one so repeated simulations never collide.
        if call_sid is not None:
            existing = (
                await self._session.execute(
                    select(Call).where(Call.twilio_call_sid == call_sid)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return CallStart(call=existing, greeting=greeting, language=lang)

        locale = lang.locale if lang is not Language.UNKNOWN else None
        call = Call(
            twilio_call_sid=call_sid or f"sim-{uuid4().hex}",
            from_number=from_number,
            to_number=to_number,
            language=locale,
            status="in_progress",
        )
        self._session.add(call)
        await self._session.flush()

        await self._audit.record(AuditEvent.CALL_STARTED, call_id=call.id)
        if lang is not Language.UNKNOWN:
            await self._audit.record(
                AuditEvent.LANGUAGE_DETECTED,
                call_id=call.id,
                data={"language": locale, "source": "provided"},
            )

        # Store the greeting as the assistant's opening turn (safe — no medical advice).
        self._session.add(Transcript(call_id=call.id, role="assistant", text=greeting))
        await self._session.commit()
        return CallStart(call=call, greeting=greeting, language=lang)

    async def handle_message(
        self, *, call_id: int, text: str, language: str | None = None
    ) -> MessageOutcome:
        call = await self._get_call(call_id)

        # Resolve effective language: explicit override > detected > session > uz.
        detection = self._lang.detect(text)
        if language:
            effective = language
        elif detection.language is not Language.UNKNOWN:
            effective = detection.language.locale
        else:
            effective = call.language or Language.UZ.locale

        if call.language != effective:
            call.language = effective
            await self._audit.record(
                AuditEvent.LANGUAGE_DETECTED,
                call_id=call_id,
                data={"language": effective, "mixed": detection.is_mixed},
            )

        history = self._history_for_llm(call)

        result: AIResult = await self._ai.respond(
            history=history, user_text=text, language=effective
        )

        self._session.add(Transcript(call_id=call_id, role="user", text=text))
        self._session.add(Transcript(call_id=call_id, role="assistant", text=result.reply))
        await self._audit.record(
            AuditEvent.AI_RESPONSE_GENERATED,
            call_id=call_id,
            data={
                "action": result.action.value,
                "reason_code": result.reason_code.value,
                "sources": result.sources,
            },
        )

        if result.action is not SafetyAction.ALLOW:
            await self._audit.record(
                AuditEvent.SAFETY_GUARD_TRIGGERED,
                call_id=call_id,
                data={"reason_code": result.reason_code.value, "action": result.action.value},
            )

        transferred = False
        transfer_reason = priority = transfer_status = None
        callback_required = False
        if result.transfer_requested:
            decision = await self._operator.request_transfer(
                call,
                reason=map_reason_code(result.reason_code),
                patient_phone=call.from_number,
            )
            transferred = True
            transfer_reason = decision.reason.value
            priority = decision.priority.value
            transfer_status = decision.status.value
            callback_required = decision.callback_required

        await self._session.commit()
        return MessageOutcome(
            reply=result.reply,
            action=result.action.value,
            reason_code=result.reason_code.value,
            transferred=transferred,
            language=effective,
            transfer_reason=transfer_reason,
            priority=priority,
            transfer_status=transfer_status,
            callback_required=callback_required,
            sources=result.sources,
        )

    async def end_call(self, *, call_id: int) -> Call:
        call = await self._get_call(call_id)
        if call.status == "in_progress":
            call.status = "completed"
        call.ended_at = datetime.now(timezone.utc)
        await self._session.commit()
        return call

    @staticmethod
    def _history_for_llm(call: Call) -> list[AIMessage]:
        """Conversation turns for the LLM, dropping the leading greeting.

        A conversation must start with a user turn, so any leading assistant
        message (the greeting) is excluded from the LLM context. The greeting
        still lives in the transcript.
        """
        turns = [
            AIMessage(role=t.role, content=t.text)
            for t in sorted(call.transcripts, key=lambda t: t.id)
            if t.role in ("user", "assistant")
        ]
        while turns and turns[0].role == "assistant":
            turns.pop(0)
        return turns

    async def _get_call(self, call_id: int) -> Call:
        stmt = select(Call).where(Call.id == call_id).options(selectinload(Call.transcripts))
        call = (await self._session.execute(stmt)).scalar_one_or_none()
        if call is None:
            raise ValueError(f"Call {call_id} not found")
        return call

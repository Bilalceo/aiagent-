"""Admin dashboard — read-only data endpoints for the Pilot MVP.

No auth yet (A6 adds login/roles). Handlers only delegate to services.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    build_audio_recording_service,
    build_voice_readiness_service,
    get_audio_storage,
)
from app.core.db import get_session
from app.models.admin_user import AdminUser
from app.models.audio_recording import AudioRecording
from app.schemas.admin import (
    AdminCallDetailOut,
    AdminCallOut,
    AdminStatsOut,
    AuditLogOut,
    CallbackNotesRequest,
    CallbackTaskOut,
    RescheduleRequest,
)
from app.schemas.audio import AudioRecordingDetailOut, AudioRecordingOut
from app.schemas.knowledge import KnowledgeItemCreate, KnowledgeItemOut, KnowledgeItemUpdate
from app.schemas.telephony import (
    TelephonyCallDetailOut,
    TelephonyCallOut,
    TelephonyStreamDetailOut,
    TelephonyStreamOut,
)
from app.services.admin.dashboard import AdminDashboardService
from app.services.telephony.service import TelephonyCallService
from app.services.telephony.stream import TelephonyStreamService
from app.services.voice.storage import AudioStorageError
from app.services.audit.log import AuditEvent, AuditLogService, redact_metadata
from app.services.auth.deps import require_roles
from app.services.callbacks.service import (
    CallbackError,
    CallbackNotFoundError,
    CallbackPermissionError,
    CallbackTaskService,
)
from app.services.knowledge.seed import seed_demo_clinic
from app.services.knowledge.service import KnowledgeBaseService

router = APIRouter()

# Role guards per TZ §8.5.
_STAFF = require_roles("super_admin", "admin", "operator")
_MANAGERS = require_roles("super_admin", "admin")
_SUPER = require_roles("super_admin")


@router.get("/audit-logs", response_model=list[AuditLogOut])
async def list_audit_logs(
    event_type: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
    _user: AdminUser = Depends(_MANAGERS),
) -> list[dict]:
    rows = await AuditLogService(session).list_logs(
        event_type=event_type,
        actor_user_id=actor_user_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return [
        {
            "id": r.id,
            "event_type": r.event,
            "actor_user_id": r.actor_user_id,
            "target_type": None,
            "target_id": None,
            "ip_address": None,
            "user_agent": None,
            "metadata": redact_metadata(r.data),
            "created_at": r.created_at,
        }
        for r in rows
    ]


@router.get("/stats", response_model=AdminStatsOut)
async def stats(
    session: AsyncSession = Depends(get_session),
    _user: AdminUser = Depends(_MANAGERS),
) -> dict:
    return await AdminDashboardService(session).stats()


@router.get("/calls", response_model=list[AdminCallOut])
async def list_calls(
    status: Optional[str] = None,
    language: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
    _user: AdminUser = Depends(_STAFF),
) -> list[dict]:
    return await AdminDashboardService(session).list_calls(status=status, language=language)


@router.get("/calls/{call_id}", response_model=AdminCallDetailOut)
async def call_detail(
    call_id: int,
    session: AsyncSession = Depends(get_session),
    _user: AdminUser = Depends(_STAFF),
) -> dict:
    detail = await AdminDashboardService(session).call_detail(call_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return detail


# --- Audio recordings (metadata only; no raw audio bytes) ------------------
@router.get("/audio-recordings", response_model=list[AudioRecordingOut])
async def list_audio_recordings(
    call_id: Optional[int] = None,
    direction: Optional[str] = None,
    kind: Optional[str] = None,
    include_deleted: bool = False,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
    _user: AdminUser = Depends(_MANAGERS),
) -> list[AudioRecording]:
    svc = build_audio_recording_service(session)
    return await svc.list(
        call_session_id=call_id,
        direction=direction,
        kind=kind,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )


@router.get("/audio-recordings/{recording_id}", response_model=AudioRecordingDetailOut)
async def get_audio_recording(
    recording_id: int,
    session: AsyncSession = Depends(get_session),
    _user: AdminUser = Depends(_MANAGERS),
) -> AudioRecordingDetailOut:
    svc = build_audio_recording_service(session)
    rec = await svc.get(recording_id, include_deleted=False)
    if rec is None:
        raise HTTPException(status_code=404, detail="Audio recording not found")
    out = AudioRecordingDetailOut.model_validate(rec)
    # Placeholder signed URL; ephemeral mock storage may not resolve it.
    try:
        out.signed_url = await get_audio_storage().get_signed_url(rec.storage_key)
    except AudioStorageError:
        out.signed_url = None
    return out


@router.post("/audio-recordings/{recording_id}/delete", response_model=AudioRecordingOut)
async def delete_audio_recording(
    recording_id: int,
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(_MANAGERS),
) -> AudioRecording:
    svc = build_audio_recording_service(session)
    rec = await svc.get(recording_id, include_deleted=False)
    if rec is None:
        raise HTTPException(status_code=404, detail="Audio recording not found")
    rec = await svc.soft_delete(recording_id)
    await AuditLogService(session).record(
        AuditEvent.AUDIO_RECORDING_DELETED,
        call_id=rec.call_session_id,
        actor=actor.email,
        actor_user_id=actor.id,
        data={"recording_id": recording_id, "storage_key": rec.storage_key},
    )
    await session.commit()
    await session.refresh(rec)
    return rec


# --- Telephony intake (read-only; super_admin/admin) -----------------------
def _telephony_out(rec) -> TelephonyCallOut:
    """Serialize a TelephonyCall, redacting any sensitive-looking metadata keys."""
    out = TelephonyCallOut.model_validate(rec)
    out.raw_metadata = redact_metadata(rec.raw_metadata)
    return out


@router.get("/telephony-calls", response_model=list[TelephonyCallOut])
async def list_telephony_calls(
    provider: Optional[str] = None,
    status: Optional[str] = None,
    direction: Optional[str] = None,
    call_session_id: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
    _user: AdminUser = Depends(_MANAGERS),
) -> list[TelephonyCallOut]:
    rows = await TelephonyCallService(session).list(
        provider=provider,
        status=status,
        direction=direction,
        call_session_id=call_session_id,
        limit=limit,
        offset=offset,
    )
    return [_telephony_out(r) for r in rows]


@router.get("/telephony-calls/{telephony_call_id}", response_model=TelephonyCallDetailOut)
async def get_telephony_call(
    telephony_call_id: int,
    session: AsyncSession = Depends(get_session),
    _user: AdminUser = Depends(_MANAGERS),
) -> TelephonyCallOut:
    rec = await TelephonyCallService(session).get(telephony_call_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Telephony call not found")
    return _telephony_out(rec)


# --- Telephony media streams (read-only; super_admin/admin) ----------------
@router.get("/telephony-streams", response_model=list[TelephonyStreamOut])
async def list_telephony_streams(
    call_sid: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
    _user: AdminUser = Depends(_MANAGERS),
):
    return await TelephonyStreamService(session).list(
        call_sid=call_sid, status=status, limit=limit, offset=offset
    )


@router.get("/voice-provider-readiness")
async def voice_provider_readiness(
    _user: AdminUser = Depends(_MANAGERS),
):
    """Config-only readiness for the real live-voice pipeline (A31). No network
    calls; never reveals the Deepgram key or the smoke token."""
    return build_voice_readiness_service().check()


@router.get("/telephony-streams/{stream_id}", response_model=TelephonyStreamDetailOut)
async def get_telephony_stream(
    stream_id: int,
    session: AsyncSession = Depends(get_session),
    _user: AdminUser = Depends(_MANAGERS),
):
    rec = await TelephonyStreamService(session).get(stream_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Telephony stream not found")
    return rec


def _cb_raise(exc: CallbackError):
    if isinstance(exc, CallbackNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, CallbackPermissionError):
        raise HTTPException(status_code=403, detail=str(exc))
    raise HTTPException(status_code=400, detail=str(exc))


async def _cb_audit(session: AsyncSession, event: AuditEvent, actor: AdminUser, task_id: int) -> None:
    await AuditLogService(session).record(
        event, actor=actor.email, actor_user_id=actor.id,
        data={"actor_user_id": actor.id, "callback_id": task_id},
    )


@router.get("/callbacks", response_model=list[CallbackTaskOut])
async def list_callbacks(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    reason: Optional[str] = None,
    assigned_to_me: bool = False,
    session: AsyncSession = Depends(get_session),
    user: AdminUser = Depends(_STAFF),
):
    return await CallbackTaskService(session).list(
        status=status, priority=priority, reason=reason,
        assigned_to_me=assigned_to_me, current_user_id=user.id,
    )


@router.post("/callbacks/{task_id}/assign", response_model=CallbackTaskOut)
async def assign_callback(
    task_id: int, session: AsyncSession = Depends(get_session), user: AdminUser = Depends(_STAFF)
):
    try:
        task = await CallbackTaskService(session).assign(task_id, user)
    except CallbackError as exc:
        _cb_raise(exc)
    await _cb_audit(session, AuditEvent.CALLBACK_ASSIGNED, user, task_id)
    await session.commit()
    await session.refresh(task)
    return task


@router.post("/callbacks/{task_id}/complete", response_model=CallbackTaskOut)
async def complete_callback(
    task_id: int, session: AsyncSession = Depends(get_session), user: AdminUser = Depends(_STAFF)
):
    try:
        task = await CallbackTaskService(session).complete(task_id, user)
    except CallbackError as exc:
        _cb_raise(exc)
    await _cb_audit(session, AuditEvent.CALLBACK_COMPLETED, user, task_id)
    await session.commit()
    await session.refresh(task)
    return task


@router.post("/callbacks/{task_id}/cancel", response_model=CallbackTaskOut)
async def cancel_callback(
    task_id: int, session: AsyncSession = Depends(get_session), user: AdminUser = Depends(_MANAGERS)
):
    try:
        task = await CallbackTaskService(session).cancel(task_id, user)
    except CallbackError as exc:
        _cb_raise(exc)
    await _cb_audit(session, AuditEvent.CALLBACK_CANCELLED, user, task_id)
    await session.commit()
    await session.refresh(task)
    return task


@router.post("/callbacks/{task_id}/reschedule", response_model=CallbackTaskOut)
async def reschedule_callback(
    task_id: int,
    payload: RescheduleRequest,
    session: AsyncSession = Depends(get_session),
    user: AdminUser = Depends(_MANAGERS),
):
    try:
        task = await CallbackTaskService(session).reschedule(task_id, payload.due_at)
    except CallbackError as exc:
        _cb_raise(exc)
    await _cb_audit(session, AuditEvent.CALLBACK_RESCHEDULED, user, task_id)
    await session.commit()
    await session.refresh(task)
    return task


@router.patch("/callbacks/{task_id}/notes", response_model=CallbackTaskOut)
async def update_callback_notes(
    task_id: int,
    payload: CallbackNotesRequest,
    session: AsyncSession = Depends(get_session),
    user: AdminUser = Depends(_STAFF),
):
    try:
        task = await CallbackTaskService(session).update_notes(task_id, user, payload.resolution_notes)
    except CallbackError as exc:
        _cb_raise(exc)
    await _cb_audit(session, AuditEvent.CALLBACK_NOTES_UPDATED, user, task_id)
    await session.commit()
    await session.refresh(task)
    return task


@router.get("/knowledge-items", response_model=list[KnowledgeItemOut])
async def list_knowledge_items(
    category: Optional[str] = None,
    active_only: bool = False,
    session: AsyncSession = Depends(get_session),
    _user: AdminUser = Depends(_MANAGERS),
):
    return await KnowledgeBaseService(session).list(category=category, active_only=active_only)


@router.post("/knowledge/seed")
async def seed(
    session: AsyncSession = Depends(get_session),
    user: AdminUser = Depends(_SUPER),
) -> dict:
    count = await seed_demo_clinic(session)
    await AuditLogService(session).record(
        AuditEvent.ADMIN_ACTION,
        actor=user.email,
        actor_user_id=user.id,
        data={"action": "knowledge_seed", "user_id": user.id, "inserted": count},
    )
    await session.commit()
    return {"status": "ok", "inserted": count}


# --- KB mutations (super_admin, admin) --------------------------------------
async def _kb_audit(session: AsyncSession, event: AuditEvent, actor: AdminUser, item_id: int) -> None:
    await AuditLogService(session).record(
        event, actor=actor.email, actor_user_id=actor.id,
        data={"actor_user_id": actor.id, "item_id": item_id},
    )


@router.post("/knowledge-items", response_model=KnowledgeItemOut, status_code=201)
async def create_knowledge_item(
    payload: KnowledgeItemCreate,
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(_MANAGERS),
):
    item = await KnowledgeBaseService(session).create(
        category=payload.category,
        title=payload.title,
        content_uz=payload.content_uz,
        content_ru=payload.content_ru,
        tags=payload.tags,
        is_active=payload.is_active,
    )
    await _kb_audit(session, AuditEvent.KNOWLEDGE_ITEM_CREATED, actor, item.id)
    await session.commit()
    await session.refresh(item)
    return item


@router.patch("/knowledge-items/{item_id}", response_model=KnowledgeItemOut)
async def update_knowledge_item(
    item_id: int,
    payload: KnowledgeItemUpdate,
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(_MANAGERS),
):
    item = await KnowledgeBaseService(session).update(
        item_id, **payload.model_dump(exclude_unset=True)
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    await _kb_audit(session, AuditEvent.KNOWLEDGE_ITEM_UPDATED, actor, item_id)
    await session.commit()
    await session.refresh(item)
    return item


@router.post("/knowledge-items/{item_id}/activate", response_model=KnowledgeItemOut)
async def activate_knowledge_item(
    item_id: int,
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(_MANAGERS),
):
    item = await KnowledgeBaseService(session).update(item_id, is_active=True)
    if item is None:
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    await _kb_audit(session, AuditEvent.KNOWLEDGE_ITEM_ACTIVATED, actor, item_id)
    await session.commit()
    await session.refresh(item)
    return item


@router.post("/knowledge-items/{item_id}/deactivate", response_model=KnowledgeItemOut)
async def deactivate_knowledge_item(
    item_id: int,
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(_MANAGERS),
):
    item = await KnowledgeBaseService(session).update(item_id, is_active=False)
    if item is None:
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    await _kb_audit(session, AuditEvent.KNOWLEDGE_ITEM_DEACTIVATED, actor, item_id)
    await session.commit()
    await session.refresh(item)
    return item


@router.delete("/knowledge-items/{item_id}")
async def delete_knowledge_item(
    item_id: int,
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(_MANAGERS),
) -> dict:
    ok = await KnowledgeBaseService(session).delete(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    await _kb_audit(session, AuditEvent.KNOWLEDGE_ITEM_DELETED, actor, item_id)
    await session.commit()
    return {"status": "deleted", "id": item_id}

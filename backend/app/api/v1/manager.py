"""Manager dashboard endpoints (M1a) - manager-safe, role-gated.

Reuses AdminDashboardService but returns ONLY non-technical, manager-safe data
(no voice/provider/stream/latency/transcript fields). Phone numbers are masked
server-side. Gated to manager / admin / super_admin (operators are excluded).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.models.admin_user import AdminUser
from app.schemas.manager import ManagerActionItemOut, ManagerCallOut, ManagerStatsOut
from app.services.admin.dashboard import AdminDashboardService
from app.services.auth.deps import require_roles
from app.services.voice.live_call import redact_number

router = APIRouter()

# Manager dashboard is for the clinic manager/owner; admins/super_admins also see it.
_MANAGER = require_roles("manager", "admin", "super_admin")

# Callback statuses that still need manager/operator attention.
_OPEN_CALLBACK = ("callback_required", "assigned")


def _mask_phone(raw: Optional[str]) -> Optional[str]:
    """Mask a phone for manager display (reuses the shared redact_number helper)."""
    masked = redact_number(raw)
    return masked or None


@router.get("/stats", response_model=ManagerStatsOut)
async def manager_stats(
    session: AsyncSession = Depends(get_session),
    _user: AdminUser = Depends(_MANAGER),
) -> ManagerStatsOut:
    s = await AdminDashboardService(session).stats()
    return ManagerStatsOut(
        total_calls=s["total_calls"],
        ai_resolved=s["ai_resolved"],
        operator_transfers=s["operator_transfers"],
        callbacks_required=s["callbacks_required"],
        kb_items=s["kb_items"],
    )


@router.get("/action-items", response_model=list[ManagerActionItemOut])
async def manager_action_items(
    session: AsyncSession = Depends(get_session),
    _user: AdminUser = Depends(_MANAGER),
) -> list[ManagerActionItemOut]:
    rows = await AdminDashboardService(session).list_callbacks(limit=100)
    return [
        ManagerActionItemOut(
            id=r["id"],
            call_session_id=r["call_session_id"],
            reason=r["reason"],
            priority=r["priority"],
            status=r["status"],
            due_at=r["due_at"],
            phone_masked=_mask_phone(r["patient_phone"]),
            created_at=r["created_at"],
        )
        for r in rows
        if r["status"] in _OPEN_CALLBACK
    ]


@router.get("/recent-calls", response_model=list[ManagerCallOut])
async def manager_recent_calls(
    limit: int = 10,
    session: AsyncSession = Depends(get_session),
    _user: AdminUser = Depends(_MANAGER),
) -> list[ManagerCallOut]:
    capped = max(1, min(limit, 50))
    rows = await AdminDashboardService(session).list_calls(limit=capped)
    return [
        ManagerCallOut(
            id=r["id"],
            from_masked=_mask_phone(r["from_number"]),
            language=r["language"],
            status=r["status"],
            started_at=r["started_at"],
            duration_seconds=r["duration_seconds"],
        )
        for r in rows
    ]

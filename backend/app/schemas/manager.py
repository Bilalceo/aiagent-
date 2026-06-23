"""Manager dashboard response schemas (M1a) - manager-safe fields only.

No voice/provider/stream/latency/transcript fields. Phone numbers are masked.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ManagerStatsOut(BaseModel):
    total_calls: int
    ai_resolved: int
    operator_transfers: int
    callbacks_required: int
    kb_items: int


class ManagerActionItemOut(BaseModel):
    id: int
    call_session_id: int
    reason: str
    priority: str
    status: str
    due_at: Optional[datetime] = None
    phone_masked: Optional[str] = None
    created_at: Optional[datetime] = None


class ManagerCallOut(BaseModel):
    id: int
    from_masked: Optional[str] = None
    language: Optional[str] = None
    status: str
    started_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None

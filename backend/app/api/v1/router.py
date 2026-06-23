"""v1 router'larini yig'adi."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    auth,
    bookings,
    calls,
    health,
    manager,
    simulation,
    telephony,
    users,
    voice,
)

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(simulation.router, prefix="/simulation", tags=["simulation"])
api_router.include_router(voice.router, prefix="/voice", tags=["voice"])
api_router.include_router(telephony.router, prefix="/telephony", tags=["telephony"])
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(users.router, prefix="/admin/users", tags=["admin-users"])
api_router.include_router(calls.router, prefix="/calls", tags=["calls"])
api_router.include_router(bookings.router, prefix="/bookings", tags=["bookings"])
api_router.include_router(manager.router, prefix="/manager", tags=["manager"])

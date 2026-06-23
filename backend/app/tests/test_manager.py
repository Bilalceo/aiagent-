"""Manager dashboard endpoints (M1a) - role gating + manager-safe output.

Verifies: manager role is valid; manager/admin/super_admin can read the manager
endpoints; operator is forbidden; unauthenticated is rejected; responses carry no
raw phone (masked) and no technical voice/provider fields.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.main import app
from app.models.callback_task import CallbackTask
from app.services.auth.service import AuthService

API = "/api/v1"


async def _login(c: AsyncClient, email: str, password: str) -> None:
    r = await c.post(f"{API}/auth/login", json={"email": email, "password": password})
    c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"


@pytest_asyncio.fixture
async def app_client(db_session: AsyncSession):
    async def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, db_session
    app.dependency_overrides.clear()


# --- role validity -----------------------------------------------------------
@pytest.mark.asyncio
async def test_manager_is_a_valid_role(db_session: AsyncSession) -> None:
    user = await AuthService(db_session).create_user(
        email="m@clinic.uz", password="managerpw", full_name="M", role="manager"
    )
    assert user.role == "manager"


@pytest.mark.asyncio
async def test_invalid_role_still_rejected(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await AuthService(db_session).create_user(
            email="x@clinic.uz", password="pw", full_name="X", role="director"
        )


# --- access control ----------------------------------------------------------
@pytest.mark.asyncio
async def test_manager_can_access(app_client) -> None:
    c, db = app_client
    await AuthService(db).create_user(email="mgr@clinic.uz", password="managerpw", full_name="Mgr", role="manager")
    await db.commit()
    await _login(c, "mgr@clinic.uz", "managerpw")
    for path in ("/manager/stats", "/manager/action-items", "/manager/recent-calls"):
        r = await c.get(f"{API}{path}")
        assert r.status_code == 200, path


@pytest.mark.asyncio
async def test_admin_can_access_manager(app_client) -> None:
    c, db = app_client
    await AuthService(db).create_user(email="a@clinic.uz", password="adminpw", full_name="A", role="admin")
    await db.commit()
    await _login(c, "a@clinic.uz", "adminpw")
    assert (await c.get(f"{API}/manager/stats")).status_code == 200


@pytest.mark.asyncio
async def test_operator_forbidden(app_client) -> None:
    c, db = app_client
    await AuthService(db).create_user(email="op@clinic.uz", password="operatorpw", full_name="Op", role="operator")
    await db.commit()
    await _login(c, "op@clinic.uz", "operatorpw")
    assert (await c.get(f"{API}/manager/stats")).status_code == 403


@pytest.mark.asyncio
async def test_unauthenticated_rejected(app_client) -> None:
    c, _ = app_client
    assert (await c.get(f"{API}/manager/stats")).status_code in (401, 403)


# --- manager-safe output -----------------------------------------------------
@pytest.mark.asyncio
async def test_stats_has_only_safe_counts(app_client) -> None:
    c, db = app_client
    await AuthService(db).create_user(email="m2@clinic.uz", password="managerpw", full_name="M2", role="manager")
    await db.commit()
    await _login(c, "m2@clinic.uz", "managerpw")
    body = (await c.get(f"{API}/manager/stats")).json()
    assert set(body) == {"total_calls", "ai_resolved", "operator_transfers", "callbacks_required", "kb_items"}
    # no technical voice/provider/latency fields leak
    assert not any(k in str(body) for k in ("deepgram", "latency", "stream", "transcript", "smoke"))


@pytest.mark.asyncio
async def test_action_items_phone_masked(app_client) -> None:
    c, db = app_client
    await AuthService(db).create_user(email="m3@clinic.uz", password="managerpw", full_name="M3", role="manager")
    db.add(CallbackTask(
        call_session_id=1, patient_phone="+998901112233", reason="operator_request",
        priority="high", status="callback_required",
    ))
    await db.commit()
    await _login(c, "m3@clinic.uz", "managerpw")
    items = (await c.get(f"{API}/manager/action-items")).json()
    assert len(items) == 1
    assert items[0]["phone_masked"] and "+998901112233" not in str(items[0])
    assert "patient_phone" not in items[0]  # raw field never exposed

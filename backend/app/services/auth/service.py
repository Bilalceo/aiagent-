"""AuthService — user lookup, creation, and credential verification."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_user import AdminUser
from app.services.auth.password import hash_password, verify_password

VALID_ROLES = ("super_admin", "admin", "operator", "manager")


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_email(self, email: str) -> Optional[AdminUser]:
        stmt = select(AdminUser).where(AdminUser.email == email.lower())
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, user_id: int) -> Optional[AdminUser]:
        return await self._session.get(AdminUser, user_id)

    async def create_user(
        self,
        *,
        email: str,
        password: str,
        full_name: Optional[str],
        role: str,
        is_active: bool = True,
    ) -> AdminUser:
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role}")
        user = AdminUser(
            email=email.lower(),
            full_name=full_name,
            password_hash=hash_password(password),
            role=role,
            is_active=is_active,
        )
        self._session.add(user)
        await self._session.flush()
        return user

    async def authenticate(self, email: str, password: str) -> Optional[AdminUser]:
        """Return the user on success; None if missing, wrong password, or inactive."""
        user = await self.get_by_email(email)
        if user is None or not user.is_active:
            return None
        if not verify_password(password, user.password_hash):
            return None
        user.last_login_at = datetime.now(timezone.utc)
        await self._session.flush()
        return user

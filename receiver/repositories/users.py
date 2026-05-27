"""User persistence operations.

Handles user creation, lookup by email, and profile updates. Password
hashing is done in the password module, not here — this layer stores
and retrieves the hash string, nothing more.

All functions take an AsyncSession and never commit. The surrounding
context (request handler or background task) owns the commit.
"""

from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import User

logger = logging.getLogger("strathon.receiver.repositories.users")


async def find_by_email(session: AsyncSession, email: str) -> Optional[User]:
    """Case-insensitive email lookup. Returns None if not found."""
    stmt = (
        select(User)
        .where(func.lower(User.email) == email.lower().strip())
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def find_by_id(session: AsyncSession, user_id: UUID) -> Optional[User]:
    """Fetch a user by primary key."""
    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password_hash: str,
    display_name: Optional[str] = None,
) -> User:
    """Insert a new user. Caller must validate email uniqueness first."""
    user = User(
        email=email.lower().strip(),
        password_hash=password_hash,
        display_name=display_name or email.split("@")[0],
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def touch_last_login(session: AsyncSession, user_id: UUID) -> None:
    """Update last_login_at to NOW()."""
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(last_login_at=func.now())
    )
    await session.execute(stmt)


async def update_password_hash(
    session: AsyncSession, user_id: UUID, new_hash: str
) -> None:
    """Replace the stored password hash (for rehash-on-login)."""
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(password_hash=new_hash)
    )
    await session.execute(stmt)


async def count_users(session: AsyncSession) -> int:
    """Return total user count. Used to detect first-user registration."""
    stmt = select(func.count()).select_from(User)
    result = await session.execute(stmt)
    return result.scalar_one()

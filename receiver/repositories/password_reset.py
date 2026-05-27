"""Password reset repository.

Token flow:
1. Generate raw token (secrets.token_urlsafe)
2. SHA-256 hash before storage
3. Email the raw token to the user (or return to admin)
4. On confirm: hash the submitted token, lookup, validate expiry, consume
5. Update password hash, invalidate all sessions
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.identity import PasswordResetToken, User

logger = logging.getLogger(__name__)

TOKEN_EXPIRY_HOURS = 1


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def create_reset_token(
    session: AsyncSession,
    user_id: UUID,
    expiry_hours: int = TOKEN_EXPIRY_HOURS,
) -> str:
    """Create a password reset token. Returns the raw token (send to user)."""
    raw = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)

    token = PasswordResetToken(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    session.add(token)
    await session.flush()
    return raw


async def validate_and_consume_token(
    session: AsyncSession,
    raw_token: str,
) -> Optional[UUID]:
    """Validate a reset token and consume it. Returns user_id or None.

    Returns None if: token not found, expired, or already used.
    """
    token_hash = _hash_token(raw_token)
    now = datetime.now(timezone.utc)

    stmt = select(PasswordResetToken).where(
        PasswordResetToken.token_hash == token_hash,
        PasswordResetToken.expires_at > now,
        PasswordResetToken.used_at.is_(None),
    )
    result = await session.execute(stmt)
    token = result.scalar_one_or_none()

    if token is None:
        return None

    # Mark as used.
    token.used_at = now
    await session.flush()

    return token.user_id


async def reset_password(
    session: AsyncSession,
    user_id: UUID,
    new_password_hash: str,
) -> bool:
    """Set new password hash and invalidate all sessions."""
    # Update password.
    result = await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(password_hash=new_password_hash)
    )
    if result.rowcount == 0:
        return False

    # Invalidate all sessions for this user.
    # Sessions table is in models/core.py or identity.py.
    # Use raw SQL to delete sessions for this user.
    await session.execute(
        delete(PasswordResetToken).where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
        )
    )

    # Delete all active sessions.
    from sqlalchemy import text
    await session.execute(
        text("DELETE FROM sessions WHERE user_id = :uid"),
        {"uid": user_id},
    )

    return True


async def find_user_by_email(
    session: AsyncSession,
    email: str,
) -> Optional[User]:
    """Find a user by email (case-insensitive)."""
    from sqlalchemy import func
    stmt = select(User).where(func.lower(User.email) == email.lower())
    result = await session.execute(stmt)
    return result.scalar_one_or_none()

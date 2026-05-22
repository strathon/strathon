"""Account lockout and concurrent session management.

Auto-activate features that work immediately after deployment.

Account lockout:
  After 5 failed login attempts, lock the account for 15 minutes.
  Resets on successful login. Configurable via env vars.

Concurrent session cap:
  Max 10 active sessions per user. On new login, if cap is reached,
  oldest session is evicted. Configurable via env var.

Research: OWASP ASVS V2.2.1 (account lockout), NIST 800-63B
(throttling mechanism), industry standard 5 attempts / 15 min lock.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("strathon.security")

# Configurable via env vars.
MAX_FAILED_ATTEMPTS = int(os.environ.get("STRATHON_LOCKOUT_ATTEMPTS", "5"))
LOCKOUT_MINUTES = int(os.environ.get("STRATHON_LOCKOUT_MINUTES", "15"))
MAX_CONCURRENT_SESSIONS = int(os.environ.get("STRATHON_MAX_SESSIONS", "10"))


async def check_account_lockout(session: AsyncSession, email: str) -> str | None:
    """Check if account is locked. Returns error message or None if OK."""
    result = await session.execute(
        text(
            "SELECT failed_login_attempts, locked_until "
            "FROM users WHERE LOWER(email) = LOWER(:email)"
        ),
        {"email": email},
    )
    row = result.first()
    if row is None:
        return None  # User not found — let login handle the error.

    locked_until = row[1]
    if locked_until and locked_until > datetime.now(timezone.utc):
        remaining = (locked_until - datetime.now(timezone.utc)).seconds // 60
        return (
            f"Account temporarily locked. Try again in {remaining + 1} minutes."
        )

    return None


async def record_failed_login(session: AsyncSession, email: str) -> None:
    """Increment failed attempts. Lock account if threshold reached."""
    result = await session.execute(
        text(
            "UPDATE users SET failed_login_attempts = failed_login_attempts + 1 "
            "WHERE LOWER(email) = LOWER(:email) "
            "RETURNING failed_login_attempts"
        ),
        {"email": email},
    )
    row = result.first()
    if row and row[0] >= MAX_FAILED_ATTEMPTS:
        lock_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
        await session.execute(
            text(
                "UPDATE users SET locked_until = :lock_until "
                "WHERE LOWER(email) = LOWER(:email)"
            ),
            {"email": email, "lock_until": lock_until},
        )
        logger.warning("Account locked: %s (after %d failed attempts)", email, row[0])
    await session.commit()


async def reset_failed_login(session: AsyncSession, email: str) -> None:
    """Reset failed attempts and unlock after successful login."""
    await session.execute(
        text(
            "UPDATE users SET failed_login_attempts = 0, locked_until = NULL "
            "WHERE LOWER(email) = LOWER(:email)"
        ),
        {"email": email},
    )


async def enforce_session_cap(session: AsyncSession, user_id) -> int:
    """Evict oldest sessions if user exceeds concurrent cap.

    Returns number of sessions evicted.
    """
    result = await session.execute(
        text(
            "SELECT COUNT(*) FROM sessions WHERE user_id = :uid "
            "AND expires_at > NOW()"
        ),
        {"uid": user_id},
    )
    count = result.scalar() or 0

    if count < MAX_CONCURRENT_SESSIONS:
        return 0

    # Evict oldest sessions to make room.
    evict_count = count - MAX_CONCURRENT_SESSIONS + 1
    await session.execute(
        text(
            "DELETE FROM sessions WHERE id IN ("
            "  SELECT id FROM sessions WHERE user_id = :uid "
            "  AND expires_at > NOW() "
            "  ORDER BY created_at ASC LIMIT :n"
            ")"
        ),
        {"uid": user_id, "n": evict_count},
    )
    logger.info("Evicted %d sessions for user %s (cap: %d)", evict_count, user_id, MAX_CONCURRENT_SESSIONS)
    return evict_count

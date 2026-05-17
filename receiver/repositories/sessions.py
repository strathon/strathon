"""Session persistence for dashboard authentication.

Sessions use opaque tokens (secrets.token_urlsafe) stored as SHA-256
hashes — same pattern as API keys. The token is returned to the user
once at login and never stored in plaintext.

Session lifecycle:
    login  → create session row, return token
    auth   → hash incoming token, lookup by hash, check expiry
    logout → delete session row

The sessions table was created in migration 001 with: id, user_id,
token_hash, expires_at, created_at, ip_address, user_agent.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Session

logger = logging.getLogger("strathon.receiver.repositories.sessions")

# Session tokens are 64 bytes of randomness → 86 base64url characters.
# Higher entropy than API keys because sessions are shorter-lived and
# the token is the sole credential (no prefix-based lookup optimization).
_TOKEN_RANDOM_BYTES = 64
_DEFAULT_SESSION_TTL_HOURS = 24


def _sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_session(
    session: AsyncSession,
    *,
    user_id: UUID,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    ttl_hours: int = _DEFAULT_SESSION_TTL_HOURS,
) -> tuple[str, Session]:
    """Create a new session. Returns (raw_token, Session).

    The raw_token MUST be returned to the user exactly once. Only the
    SHA-256 hash is persisted.
    """
    raw_token = secrets.token_urlsafe(_TOKEN_RANDOM_BYTES)
    token_hash = _sha256_hex(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)

    sess = Session(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    session.add(sess)
    await session.flush()
    await session.refresh(sess)
    return raw_token, sess


async def resolve_session_token(
    session: AsyncSession,
    token: str,
) -> Optional[Session]:
    """Resolve a raw session token to its Session row.

    Returns None if the token doesn't match or the session is expired.
    Expired sessions are not deleted here — a periodic cleanup task
    handles that.
    """
    token_hash = _sha256_hex(token)
    stmt = (
        select(Session)
        .where(Session.token_hash == token_hash)
        .where(Session.expires_at > datetime.now(timezone.utc))
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def delete_session(session: AsyncSession, session_id: UUID) -> bool:
    """Delete a session by ID (logout). Returns True if a row was deleted."""
    stmt = delete(Session).where(Session.id == session_id)
    result = await session.execute(stmt)
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def delete_all_user_sessions(session: AsyncSession, user_id: UUID) -> int:
    """Delete all sessions for a user (force logout everywhere)."""
    stmt = delete(Session).where(Session.user_id == user_id)
    result = await session.execute(stmt)
    return result.rowcount  # type: ignore[attr-defined]


async def cleanup_expired(session: AsyncSession) -> int:
    """Delete expired sessions. Returns count deleted."""
    stmt = delete(Session).where(Session.expires_at <= datetime.now(timezone.utc))
    result = await session.execute(stmt)
    count: Any = result.rowcount  # type: ignore[attr-defined]
    if count:
        logger.info("cleaned up %d expired sessions", count)
    return count

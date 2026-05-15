"""API key persistence operations.

Session-aware replacements for the raw-asyncpg functions in receiver/auth.py.
The pure helper functions (key generation, hashing, header parsing) stay in
auth.py since they don't touch the DB. This module owns everything that
hits the api_keys table.

Lookup performance:
    The hot-path function is resolve_api_key. It runs a single indexed
    query on key_prefix, then a constant-time hmac.compare_digest. The
    partial index idx_api_keys_prefix (WHERE revoked_at IS NULL) makes
    this O(1) for valid keys regardless of total key count. Revoked keys
    are not in the index, so they're equivalent to non-existent keys
    from the lookup's perspective — no leaking of "this used to be a
    valid key" timing.

Transaction model:
    These functions never call session.commit(). The surrounding context
    (the request via get_db_session, or a background task with its own
    `async with async_session_maker()`) owns the commit decision.
"""

from __future__ import annotations

import hmac
import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth import _sha256_hex, generate_api_key  # pure helpers, no DB
from models import ApiKey
from schemas.api_keys import ApiKeyCreateResponse, ApiKeyRead

logger = logging.getLogger("strathon.receiver.repositories.auth")


# ---- Read paths ----------------------------------------------------------


async def find_active_keys_by_prefix(
    session: AsyncSession, prefix: str
) -> list[ApiKey]:
    """Look up all non-revoked API keys matching a prefix.

    Returns the raw ORM rows because resolve_api_key needs to do the
    hmac.compare_digest against key_hash itself. Caller MUST be inside
    a session context — these objects become detached the moment the
    session closes.
    """
    stmt = (
        select(ApiKey)
        .where(ApiKey.key_prefix == prefix)
        .where(ApiKey.revoked_at.is_(None))
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def touch_last_used(session: AsyncSession, key_id: UUID) -> None:
    """Best-effort `last_used_at = NOW()`. Caller wraps in try/except.

    Doesn't commit — the surrounding request's commit handles it. If the
    request body itself fails for other reasons, last_used_at rolls back
    along with it, which is correct (we don't want to claim a key was
    used to authenticate a request that errored before reaching the
    handler).
    """
    from sqlalchemy import func
    stmt = (
        update(ApiKey)
        .where(ApiKey.id == key_id)
        .values(last_used_at=func.now())
    )
    await session.execute(stmt)


async def list_api_keys(
    session: AsyncSession,
    project_id: UUID,
    include_revoked: bool = False,
) -> list[ApiKeyRead]:
    """List api keys for a project, newest first.

    Ordering: created_at DESC, then id DESC as a stable tiebreaker. Without
    the tiebreaker, multiple keys created in the same transaction (or even
    the same millisecond) would have ambiguous order.
    """
    stmt = select(ApiKey).where(ApiKey.project_id == project_id)
    if not include_revoked:
        stmt = stmt.where(ApiKey.revoked_at.is_(None))
    stmt = stmt.order_by(ApiKey.created_at.desc(), ApiKey.id.desc())

    result = await session.execute(stmt)
    keys = result.scalars().all()
    return [ApiKeyRead.model_validate(k) for k in keys]


# ---- Write paths ---------------------------------------------------------


async def create_api_key(
    session: AsyncSession,
    project_id: UUID,
    name: str,
) -> ApiKeyCreateResponse:
    """Create an API key. Returns the raw key ONCE — never recoverable after.

    The raw key is generated and immediately discarded after returning;
    only its prefix and SHA-256 hash get persisted. The caller (endpoint)
    must surface raw_key in the HTTP response and instruct the user to
    store it somewhere safe.
    """
    raw, prefix, key_hash = generate_api_key()

    api_key = ApiKey(
        project_id=project_id,
        name=name,
        key_hash=key_hash,
        key_prefix=prefix,
    )
    session.add(api_key)
    # flush() forces the INSERT now so id/created_at populate from server
    # defaults before we hand back the schema. We don't commit — that's
    # the request boundary's job.
    await session.flush()
    await session.refresh(api_key)

    return ApiKeyCreateResponse(
        api_key=ApiKeyRead.model_validate(api_key),
        raw_key=raw,
    )


async def revoke_api_key(session: AsyncSession, key_id: UUID) -> bool:
    """Soft-revoke a key. Returns True iff a key was newly revoked."""
    from sqlalchemy import func

    stmt = (
        update(ApiKey)
        .where(ApiKey.id == key_id)
        .where(ApiKey.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    result = await session.execute(stmt)
    # rowcount returns the number of rows the UPDATE actually affected
    return bool(result.rowcount)


# ---- Identity verification (the hot path) -------------------------------


async def verify_token_and_touch(
    session: AsyncSession,
    token: str,
) -> Optional[ApiKey]:
    """Resolve a bearer token to the api_keys row, updating last_used_at.

    Returns None if the token doesn't match any active key. Same return
    type for "prefix didn't match anything" and "prefix matched but hash
    didn't" — this is intentional to avoid leaking the existence of
    prefixes via response timing or shape.

    The hmac.compare_digest call is constant-time. The DB query above it
    is indexed, so the whole verification is roughly the same wall-time
    regardless of whether the prefix is known.
    """
    from auth import KEY_PREFIX_LEN
    prefix = token[:KEY_PREFIX_LEN]
    incoming_hash = _sha256_hex(token)

    keys = await find_active_keys_by_prefix(session, prefix)
    for key in keys:
        if hmac.compare_digest(key.key_hash, incoming_hash):
            # Best-effort last_used_at update. Failure here mustn't deny
            # the authentication — log and continue.
            try:
                await touch_last_used(session, key.id)
            except Exception:
                logger.debug("failed to update last_used_at for key %s", key.id)
            return key

    return None

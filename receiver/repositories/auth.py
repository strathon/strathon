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

# A fixed, valid-length SHA-256 hex used only to spend one constant-time
# comparison when no key matches the prefix, so the no-match path costs the
# same as a match-but-wrong-hash path (no timing leak of prefix existence).
_DUMMY_KEY_HASH = _sha256_hex("strathon-nonexistent-key-timing-equalizer")

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
    scopes: Optional[list[str]] = None,
    expires_at: Optional[object] = None,
    allowed_ips: Optional[list[str]] = None,
) -> ApiKeyCreateResponse:
    """Create an API key. Returns the raw key ONCE — never recoverable after.

    expires_at:
        Optional datetime for hard key expiry. After this timestamp the
        key stops authenticating. Useful for temporary keys (CI, demos).
    allowed_ips:
        Optional IP allowlist. When set, requests from IPs not in this
        list are rejected with 403. Null means allow all (default).
    """
    raw, prefix, key_hash = generate_api_key()

    kwargs: dict = dict(
        project_id=project_id,
        name=name,
        key_hash=key_hash,
        key_prefix=prefix,
    )
    if scopes is not None:
        kwargs["scopes"] = scopes
    if expires_at is not None:
        kwargs["expires_at"] = expires_at
    if allowed_ips is not None:
        kwargs["allowed_ips"] = allowed_ips

    api_key = ApiKey(**kwargs)
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
    # rowcount returns the number of rows the UPDATE actually affected.
    # SQLAlchemy 2.x stubs hide it on the protocol Result; runtime
    # CursorResult exposes it.
    return bool(result.rowcount)  # type: ignore[attr-defined]


# ---- Identity verification (the hot path) -------------------------------


async def verify_token_and_touch(
    session: AsyncSession,
    token: str,
) -> Optional[ApiKey]:
    """Resolve a bearer token to the api_keys row, updating last_used_at.

    Returns None if the token doesn't match any active key, or if the
    key has expired (expires_at < now). Same return type for all failure
    modes to avoid leaking existence via timing or shape.
    """
    from auth import KEY_PREFIX_LEN
    from datetime import datetime, timezone

    prefix = token[:KEY_PREFIX_LEN]
    incoming_hash = _sha256_hex(token)

    keys = await find_active_keys_by_prefix(session, prefix)
    now = datetime.now(timezone.utc)
    for key in keys:
        if hmac.compare_digest(key.key_hash, incoming_hash):
            # Reject expired keys (treat like revoked).
            if key.expires_at is not None and key.expires_at <= now:
                logger.debug("key %s expired at %s", key.id, key.expires_at)
                return None
            try:
                await touch_last_used(session, key.id)
            except Exception:
                logger.debug("failed to update last_used_at for key %s", key.id)
            return key

    # No prefix match: still perform one comparison against a dummy hash so the
    # "no key with this prefix" path costs the same as "prefix exists but hash
    # differs". Otherwise the timing delta would reveal whether a given prefix
    # is in use (the docstring promises uniform timing across failure modes).
    hmac.compare_digest(_DUMMY_KEY_HASH, incoming_hash)
    return None


# ---- Key rotation --------------------------------------------------------


async def rotate_api_key(
    session: AsyncSession,
    key_id: UUID,
    grace_period_hours: int = 72,
) -> Optional[ApiKeyCreateResponse]:
    """Rotate a key: create a replacement, deprecate the old one.

    The old key gets deprecated_at=now and expires_at=now+grace_period.
    Both old and new keys work during the grace period. After expires_at
    the old key stops authenticating (the verify_token_and_touch check
    rejects it, and the background reaper eventually revokes it).

    Returns the new key (with raw_key shown once), or None if the
    old key was not found or already revoked/deprecated.
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func as sa_func

    # Find the old key — must be active (not revoked, not already deprecated).
    stmt = (
        select(ApiKey)
        .where(ApiKey.id == key_id)
        .where(ApiKey.revoked_at.is_(None))
        .where(ApiKey.deprecated_at.is_(None))
    )
    result = await session.execute(stmt)
    old_key = result.scalar_one_or_none()
    if old_key is None:
        return None

    now = datetime.now(timezone.utc)
    grace_delta = timedelta(hours=max(grace_period_hours, 1))

    # Deprecate the old key with a grace period.
    deprecate_stmt = (
        update(ApiKey)
        .where(ApiKey.id == key_id)
        .values(
            deprecated_at=sa_func.now(),
            expires_at=now + grace_delta,
        )
    )
    await session.execute(deprecate_stmt)

    # Create the replacement key, inheriting project + scopes.
    raw, prefix, key_hash = generate_api_key()
    new_key = ApiKey(
        project_id=old_key.project_id,
        name=f"{old_key.name} (rotated)",
        key_hash=key_hash,
        key_prefix=prefix,
        scopes=list(old_key.scopes),
        rotated_from_id=old_key.id,
    )
    session.add(new_key)
    await session.flush()
    await session.refresh(new_key)

    return ApiKeyCreateResponse(
        api_key=ApiKeyRead.model_validate(new_key),
        raw_key=raw,
    )


# ---- Key update ----------------------------------------------------------


async def update_api_key(
    session: AsyncSession,
    key_id: UUID,
    *,
    name: Optional[str] = None,
    expires_at: Optional[object] = None,  # datetime or None
) -> Optional[ApiKeyRead]:
    """Update mutable fields on an active key.

    Returns the updated key, or None if not found / already revoked.
    """
    stmt = (
        select(ApiKey)
        .where(ApiKey.id == key_id)
        .where(ApiKey.revoked_at.is_(None))
    )
    result = await session.execute(stmt)
    key = result.scalar_one_or_none()
    if key is None:
        return None

    if name is not None:
        key.name = name
    if expires_at is not None:
        key.expires_at = expires_at  # type: ignore[assignment]

    await session.flush()
    await session.refresh(key)
    return ApiKeyRead.model_validate(key)


# ---- Key reaper (background task) ----------------------------------------


async def reap_expired_keys(session: AsyncSession) -> int:
    """Revoke all keys past their expires_at. Returns count revoked."""
    from datetime import datetime, timezone
    from sqlalchemy import func as sa_func

    now = datetime.now(timezone.utc)
    stmt = (
        update(ApiKey)
        .where(ApiKey.expires_at.isnot(None))
        .where(ApiKey.expires_at <= now)
        .where(ApiKey.revoked_at.is_(None))
        .values(revoked_at=sa_func.now())
    )
    result = await session.execute(stmt)
    count = result.rowcount  # type: ignore[attr-defined]
    if count:
        logger.info("Reaped %d expired API key(s)", count)
    return count


async def find_keys_expiring_soon(
    session: AsyncSession,
    within_hours: int = 24,
) -> list[ApiKeyRead]:
    """Find active keys expiring within the given window.

    Used by the background task to emit warning webhooks.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    threshold = now + timedelta(hours=within_hours)
    stmt = (
        select(ApiKey)
        .where(ApiKey.expires_at.isnot(None))
        .where(ApiKey.expires_at > now)
        .where(ApiKey.expires_at <= threshold)
        .where(ApiKey.revoked_at.is_(None))
    )
    result = await session.execute(stmt)
    keys = result.scalars().all()
    return [ApiKeyRead.model_validate(k) for k in keys]

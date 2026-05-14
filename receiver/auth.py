"""API key authentication and project resolution for the Strathon receiver.

Key format:
    stra_<43 char base64url>            -- generated via secrets.token_urlsafe(32)
    e.g. stra_aB3xC9zD2eF1gH4iJ6kL8mN0oP2qR4sT6uV8wX0yZ

Storage:
    key_prefix: first 12 chars of the raw key (indexed for fast lookup)
    key_hash:   SHA-256 hex of the full raw key (constant-time compared)

Lookup flow:
    1. Extract Bearer token from Authorization header
    2. prefix = token[:12]
    3. SELECT id, project_id, key_hash, revoked_at
       FROM api_keys WHERE key_prefix = prefix AND revoked_at IS NULL
    4. For each match, hmac.compare_digest(sha256(token), key_hash)
    5. On match: update last_used_at async, return project_id

SHA-256 is the right choice for API keys (high-entropy secrets); bcrypt /
argon2 are for low-entropy passwords where slow hashing matters. With 256
bits of entropy in the key itself, a fast hash + indexed prefix lookup is
both secure and fast.

The /v1/api_keys CRUD endpoints are currently UNAUTHENTICATED in v1.
Production deployments MUST put the receiver behind a reverse proxy that
restricts access to those routes, OR add admin authentication (v2 work).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

import asyncpg
from fastapi import HTTPException, status

logger = logging.getLogger("strathon.receiver.auth")


KEY_PREFIX_LEN = 12
KEY_SCHEME = "stra_"
RAW_KEY_RANDOM_BYTES = 32  # secrets.token_urlsafe(32) -> 43 base64url chars


@dataclass(frozen=True)
class ApiKeyContext:
    """The resolved identity for an authenticated request."""

    key_id: UUID
    project_id: UUID
    key_prefix: str


def generate_api_key() -> tuple[str, str, str]:
    """Generate a new raw API key and its derived prefix + sha256 hash.

    Returns:
        (raw_key, key_prefix, key_hash)

    The raw_key MUST be returned to the user exactly once at creation time
    and never stored. Only the prefix and hash go to the database.
    """
    raw = f"{KEY_SCHEME}{secrets.token_urlsafe(RAW_KEY_RANDOM_BYTES)}"
    return raw, raw[:KEY_PREFIX_LEN], _sha256_hex(raw)


def _sha256_hex(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    """Pull the token from an Authorization header. Returns None if missing/malformed."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


async def resolve_api_key(
    pool: asyncpg.Pool, authorization: Optional[str]
) -> ApiKeyContext:
    """Verify the Authorization header and return the resolved project context.

    Raises HTTPException(401) on missing / malformed / unknown / revoked keys.
    """
    token = _extract_bearer_token(authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header. Expected: Bearer <api_key>",
        )

    prefix = token[:KEY_PREFIX_LEN]
    incoming_hash = _sha256_hex(token)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, project_id, key_hash
            FROM api_keys
            WHERE key_prefix = $1 AND revoked_at IS NULL
            """,
            prefix,
        )

    for row in rows:
        # Constant-time comparison so unknown-prefix and known-prefix-bad-key
        # take roughly the same time.
        if hmac.compare_digest(row["key_hash"], incoming_hash):
            # Best-effort last_used_at update — never block the request on this
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE api_keys SET last_used_at = NOW() WHERE id = $1",
                        row["id"],
                    )
            except Exception:
                logger.debug("failed to update last_used_at for key %s", row["id"])
            return ApiKeyContext(
                key_id=row["id"],
                project_id=row["project_id"],
                key_prefix=prefix,
            )

    # Either the prefix didn't match anything, or the hash didn't match any
    # row sharing that prefix. Same response either way to avoid leaking
    # which prefixes exist.
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
    )


# ---- /v1/api_keys CRUD helpers ----


async def create_api_key(
    pool: asyncpg.Pool,
    project_id: UUID,
    name: str,
) -> tuple[dict, str]:
    """Create a new API key. Returns (api_key_row, raw_key).

    The raw_key is returned ONCE and must be shown to the user immediately.
    Only the prefix and hash are stored.
    """
    raw, prefix, key_hash = generate_api_key()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO api_keys (project_id, name, key_hash, key_prefix)
            VALUES ($1, $2, $3, $4)
            RETURNING id, project_id, name, key_prefix, created_at, last_used_at, revoked_at
            """,
            project_id, name, key_hash, prefix,
        )

    return _serialize_key_row(row), raw


async def list_api_keys(
    pool: asyncpg.Pool, project_id: UUID, include_revoked: bool = False
) -> list[dict]:
    query = """
        SELECT id, project_id, name, key_prefix, created_at, last_used_at, revoked_at
        FROM api_keys
        WHERE project_id = $1
    """
    params: list = [project_id]
    if not include_revoked:
        query += " AND revoked_at IS NULL"
    query += " ORDER BY created_at DESC"

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [_serialize_key_row(r) for r in rows]


async def revoke_api_key(
    pool: asyncpg.Pool, key_id: UUID
) -> bool:
    """Soft-revoke a key. Returns True if a key was newly revoked."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE api_keys
            SET revoked_at = NOW()
            WHERE id = $1 AND revoked_at IS NULL
            """,
            key_id,
        )
    # asyncpg returns 'UPDATE n'
    try:
        return int(result.split(" ", 1)[1]) > 0
    except (IndexError, ValueError):
        return False


def _serialize_key_row(row: asyncpg.Record) -> dict:
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "name": row["name"],
        "key_prefix": row["key_prefix"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
        "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None,
    }


__all__ = [
    "ApiKeyContext",
    "create_api_key",
    "generate_api_key",
    "list_api_keys",
    "resolve_api_key",
    "revoke_api_key",
]

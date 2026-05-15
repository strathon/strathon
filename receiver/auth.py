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
    3. SELECT ... FROM api_keys WHERE key_prefix = $1 AND revoked_at IS NULL
    4. For each match, hmac.compare_digest(sha256(token), key_hash)
    5. On match: update last_used_at, return project_id

SHA-256 is the right choice for API keys (high-entropy secrets); bcrypt /
argon2 are for low-entropy passwords where slow hashing matters. With 256
bits of entropy in the key itself, a fast hash + indexed prefix lookup is
both secure and fast.

The /v1/api_keys CRUD endpoints are currently UNAUTHENTICATED in v1.
Production deployments MUST put the receiver behind a reverse proxy that
restricts access to those routes, OR add admin authentication (v2 work).

DB code lives in receiver/repositories/auth.py. This module owns:
  - Pure helpers (key generation, hashing, header parsing)
  - The public authentication entry point `resolve_api_key` that endpoints
    call. That function takes an AsyncSession; the repository layer below
    does the actual SQL.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession


KEY_PREFIX_LEN = 12
KEY_SCHEME = "stra_"
RAW_KEY_RANDOM_BYTES = 32  # secrets.token_urlsafe(32) -> 43 base64url chars


@dataclass(frozen=True)
class ApiKeyContext:
    """The resolved identity for an authenticated request."""

    key_id: UUID
    project_id: UUID
    key_prefix: str


# ---- Pure helpers (no DB) ------------------------------------------------


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


# ---- Authentication entry point -----------------------------------------


async def resolve_api_key(
    session: AsyncSession, authorization: Optional[str]
) -> ApiKeyContext:
    """Verify the Authorization header and return the resolved project context.

    Raises HTTPException(401) on missing / malformed / unknown / revoked keys.

    The session passed in is the request-scoped session from
    `Depends(get_db_session)`. The verification doesn't commit; the
    last_used_at update piggybacks on whatever transaction the surrounding
    endpoint produces.
    """
    # Imported here to avoid circular import (repositories.auth imports from this module).
    from repositories.auth import verify_token_and_touch

    token = _extract_bearer_token(authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header. Expected: Bearer <api_key>",
        )

    key = await verify_token_and_touch(session, token)
    if key is None:
        # Same response whether the prefix didn't match or the hash didn't,
        # to avoid leaking which prefixes exist.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    return ApiKeyContext(
        key_id=key.id,
        project_id=key.project_id,
        key_prefix=key.key_prefix,
    )


__all__ = [
    "ApiKeyContext",
    "KEY_PREFIX_LEN",
    "KEY_SCHEME",
    "generate_api_key",
    "resolve_api_key",
]

"""API key authentication and project resolution for the Strathon receiver.

Key format:
    stra_<43 char base64url>            -- generated via secrets.token_urlsafe(32)
    e.g. stra_aB3xC9zD2eF1gH4iJ6kL8mN0oP2qR4sT6uV8wX0yZ

Storage:
    key_prefix: first 12 chars of the raw key (indexed for fast lookup)
    key_hash:   SHA-256 hex of the full raw key (constant-time compared)
    scopes:     TEXT[] of capability names; '*' means all (wildcard)

Lookup flow:
    1. Extract Bearer token from Authorization header
    2. prefix = token[:12]
    3. SELECT ... FROM api_keys WHERE key_prefix = $1 AND revoked_at IS NULL
    4. For each match, hmac.compare_digest(sha256(token), key_hash)
    5. On match: update last_used_at, return ApiKeyContext including scopes
    6. The endpoint's require_scope dependency then checks the scope it
       declared is present in the key's scopes (or '*' is)

SHA-256 is the right choice for API keys (high-entropy secrets); bcrypt /
argon2 are for low-entropy passwords where slow hashing matters. With 256
bits of entropy in the key itself, a fast hash + indexed prefix lookup is
both secure and fast.

Scopes:
    The KNOWN_SCOPES set below is the source of truth for what scopes
    exist. Adding a new scope = add an entry there and update the
    endpoints that should accept it. No DB migration needed; scopes is a
    flat TEXT[].

    Scope strings follow the resource:action convention used by Stripe,
    PostHog, and Sentry. Wildcard '*' is the only special value.

DB code lives in receiver/repositories/auth.py. This module owns:
  - Pure helpers (key generation, hashing, header parsing)
  - Scope constants and the SCOPE_WILDCARD value
  - The public authentication entry point `resolve_api_key` that endpoints
    call. That function takes an AsyncSession; the repository layer below
    does the actual SQL.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession


KEY_PREFIX_LEN = 12
KEY_SCHEME = "stra_"
RAW_KEY_RANDOM_BYTES = 32  # secrets.token_urlsafe(32) -> 43 base64url chars


# ---- Scopes -------------------------------------------------------------
#
# resource:action naming convention, matching the pattern operators
# already know from cloud IAM (s3:GetObject, ec2:RunInstances). Adding a
# new endpoint that needs a new scope: add the scope name here, then
# declare it on the endpoint via require_scope("..."). No migration
# required — scopes is a flat TEXT[] backfilled at user-key-creation time.

SCOPE_WILDCARD = "*"

SCOPE_TRACES_WRITE = "traces:write"
SCOPE_POLICIES_READ = "policies:read"
SCOPE_POLICIES_WRITE = "policies:write"
SCOPE_API_KEYS_READ = "api_keys:read"
SCOPE_API_KEYS_WRITE = "api_keys:write"
# Webhook signing-key management (commit C2). Operators with
# webhook_signing_keys:write can create new signing secrets and revoke
# existing ones; the keystore is updated accordingly so that the next
# delivery uses the new signing material. The :read scope is enough to
# audit which keys exist without seeing any secret material.
SCOPE_WEBHOOK_SIGNING_KEYS_READ = "webhook_signing_keys:read"
SCOPE_WEBHOOK_SIGNING_KEYS_WRITE = "webhook_signing_keys:write"
# Webhook delivery inspection + replay (commit C3). The :read scope is
# enough to list and inspect deliveries (operator visibility). The
# :write scope is needed for the replay action which re-enqueues a
# previously-failed delivery and produces a fresh side-effect at the
# consumer.
SCOPE_WEBHOOK_DELIVERIES_READ = "webhook_deliveries:read"
SCOPE_WEBHOOK_DELIVERIES_WRITE = "webhook_deliveries:write"
# Halt management. :read covers operator inspection plus the
# /v1/intervention/sync endpoint that the SDK polls. :write covers
# creating and clearing halts (the operator-facing actions). The SDK
# only needs :read; humans creating kill-switches need :write.
SCOPE_HALTS_READ = "halts:read"
SCOPE_HALTS_WRITE = "halts:write"
# Budget management. :read covers GET endpoints + the
# /v1/intervention/sync endpoint that surfaces budgets to the SDK.
# :write covers POST + PATCH + DELETE of budget rows. The budget
# monitor runs in-process and uses no scope (no API key involved).
SCOPE_BUDGETS_READ = "budgets:read"
SCOPE_BUDGETS_WRITE = "budgets:write"
# Per-project model price overrides. Separate from budgets so an
# operator can grant the pricing team read+write access to prices
# without exposing the budget surface.
SCOPE_MODEL_PRICES_READ = "model_prices:read"
SCOPE_MODEL_PRICES_WRITE = "model_prices:write"

KNOWN_SCOPES: frozenset[str] = frozenset({
    SCOPE_WILDCARD,
    SCOPE_TRACES_WRITE,
    SCOPE_POLICIES_READ,
    SCOPE_POLICIES_WRITE,
    SCOPE_API_KEYS_READ,
    SCOPE_API_KEYS_WRITE,
    SCOPE_WEBHOOK_SIGNING_KEYS_READ,
    SCOPE_WEBHOOK_SIGNING_KEYS_WRITE,
    SCOPE_WEBHOOK_DELIVERIES_READ,
    SCOPE_WEBHOOK_DELIVERIES_WRITE,
    SCOPE_HALTS_READ,
    SCOPE_HALTS_WRITE,
    SCOPE_BUDGETS_READ,
    SCOPE_BUDGETS_WRITE,
    SCOPE_MODEL_PRICES_READ,
    SCOPE_MODEL_PRICES_WRITE,
})

# Default scopes for a new SDK-style key. Enough to ingest traces and to
# poll policies for SDK-side block/steer enforcement, nothing more.
DEFAULT_SDK_SCOPES: tuple[str, ...] = (SCOPE_TRACES_WRITE, SCOPE_POLICIES_READ)


def validate_scopes(scopes: list[str]) -> None:
    """Raise ValueError if any scope is unknown. Empty list is rejected.

    Called by the api_keys create endpoint before persisting.
    """
    if not scopes:
        raise ValueError("scopes must be a non-empty list")
    unknown = [s for s in scopes if s not in KNOWN_SCOPES]
    if unknown:
        raise ValueError(
            f"unknown scope(s): {sorted(unknown)}. "
            f"Known scopes: {sorted(KNOWN_SCOPES)}"
        )


def key_has_scope(key_scopes: tuple[str, ...], required: str) -> bool:
    """Wildcard-aware scope check. Used by require_scope dependency."""
    return SCOPE_WILDCARD in key_scopes or required in key_scopes


@dataclass(frozen=True)
class ApiKeyContext:
    """The resolved identity for an authenticated request."""

    key_id: UUID
    project_id: UUID
    key_prefix: str
    scopes: tuple[str, ...] = field(default_factory=tuple)


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
        scopes=tuple(key.scopes or ()),
    )


__all__ = [
    "ApiKeyContext",
    "DEFAULT_SDK_SCOPES",
    "KEY_PREFIX_LEN",
    "KEY_SCHEME",
    "KNOWN_SCOPES",
    "SCOPE_API_KEYS_READ",
    "SCOPE_API_KEYS_WRITE",
    "SCOPE_POLICIES_READ",
    "SCOPE_POLICIES_WRITE",
    "SCOPE_TRACES_WRITE",
    "SCOPE_WILDCARD",
    "generate_api_key",
    "key_has_scope",
    "resolve_api_key",
    "validate_scopes",
]

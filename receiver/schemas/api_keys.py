"""Pydantic schemas for /v1/api_keys endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ApiKeyCreate(BaseModel):
    """POST /v1/api_keys request body.

    scopes: optional list of capability scope strings. When omitted, the
    server applies the SDK-friendly default ('traces:write', 'policies:read').
    See receiver/auth.py:KNOWN_SCOPES for the full list. The wildcard
    '*' grants every scope and is intended for administrative keys
    (one-time bootstrap, CI, etc.).

    expires_at: optional hard expiry. After this timestamp the key stops
    authenticating. Useful for temporary keys (CI, demos, contractors).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    scopes: Optional[list[str]] = None
    expires_at: Optional[datetime] = None


class ApiKeyRead(BaseModel):
    """Response model for GET /v1/api_keys (list) and any non-creation read.

    Deliberately excludes `key_hash` — the hash is server-side detail that
    nothing outside the receiver should ever see, including authenticated
    operators.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    name: str
    key_prefix: str
    scopes: list[str] = Field(default_factory=list)
    created_at: datetime
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    deprecated_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    rotated_from_id: Optional[UUID] = None


class ApiKeyCreateResponse(BaseModel):
    """Response for POST /v1/api_keys.

    Includes the raw key one time and only once. After this response the
    raw key is unrecoverable — the client must store it somewhere safe.
    """

    api_key: ApiKeyRead
    raw_key: str = Field(
        description=(
            "The raw API key, shown once and never again. Store this in a "
            "password manager or secret store; it cannot be retrieved later."
        )
    )

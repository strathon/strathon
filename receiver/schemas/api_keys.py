"""Pydantic schemas for /v1/api_keys endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ApiKeyCreate(BaseModel):
    """POST /v1/api_keys request body."""

    name: str = Field(min_length=1, max_length=200)


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
    created_at: datetime
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


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

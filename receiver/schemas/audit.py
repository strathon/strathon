"""Pydantic schemas for /v1/audit endpoints.

Shapes the read and write surfaces of the audit log API:

- ``AuditEventRead`` — single event in list/get responses.
- ``AuditEventListResponse`` — paginated list with ``next_cursor``.
- ``AuditAnchorRead`` — single anchor.
- ``AuditStreamCreate`` / ``AuditStreamRead`` — webhook destination.
- ``AuditVerifyResponse`` — hash-chain verification result.

Wire format follows the research's OCSF-compatibility recommendation
in spirit (named actor/resource fields, structured before/after,
metadata for cursor); we don't ship full OCSF v1.3.0 envelope shape
in Stage 1 to keep the surface easy to evolve.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# Hard limits surfaced as API constants.
MAX_LIMIT: int = 1000
DEFAULT_LIMIT: int = 50

# Stage 1 destination types.
VALID_STREAM_KINDS = {"webhook"}


class AuditEventActor(BaseModel):
    """Actor block in an event payload."""

    type: str = Field(..., description="One of human|service_account|agent|system|anonymous")
    id: str
    display: Optional[str] = None
    on_behalf_of: Optional[str] = None


class AuditEventResource(BaseModel):
    """Resource block in an event payload."""

    type: str
    id: str
    parent: Optional[str] = None


class AuditEventRead(BaseModel):
    """One audit event as returned by the read API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sequence_no: int
    occurred_at: datetime
    ingested_at: datetime
    project_id: UUID

    actor: AuditEventActor
    action: str
    action_category: str
    outcome: str
    reason: Optional[str] = None

    resource: AuditEventResource
    cascade_root_id: Optional[UUID] = None

    request_id: UUID
    source_ip: Optional[str] = None
    user_agent: Optional[str] = None
    api_key_id: Optional[str] = None
    auth_method: Optional[str] = None

    before_state: Optional[dict[str, Any]] = None
    after_state: Optional[dict[str, Any]] = None
    diff: Optional[list[dict[str, Any]]] = None

    pii_classes: list[str] = Field(default_factory=list)
    schema_version: int = 1

    # Integrity surface (hex-encoded for JSON wire).
    prev_hash: str
    row_hash: str
    hmac_key_id: int


class AuditEventListResponse(BaseModel):
    """Paginated list of audit events."""

    data: list[AuditEventRead]
    next_cursor: Optional[str] = None


class AuditAnchorRead(BaseModel):
    """One per-interval integrity anchor."""

    anchor_at: datetime
    last_sequence: int
    last_row_hash: str  # hex
    merkle_root: str  # hex
    event_count: int
    signature: Optional[str] = None  # hex when present (Stage 2)
    signing_key_id: Optional[str] = None


class AuditAnchorListResponse(BaseModel):
    data: list[AuditAnchorRead]


class AuditVerifyResponse(BaseModel):
    """Result of /v1/audit/events/{id}/verify."""

    valid: bool
    event_id: Optional[str] = None
    sequence_no: Optional[int] = None
    hmac_key_id: Optional[int] = None
    error: Optional[str] = None


# --- Streams (webhook destinations) -------------------------------------


class AuditStreamCreate(BaseModel):
    """Body for POST /v1/audit/streams."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Operator-chosen name; unique per project.",
    )
    url: str = Field(
        ...,
        min_length=8,
        max_length=2048,
        description="HTTPS endpoint that will receive audit events.",
    )
    signing_key_id: Optional[UUID] = Field(
        default=None,
        description=(
            "Existing webhook signing key id to use for the "
            "X-Strathon-Signature header. If omitted the project's "
            "primary signing key is used."
        ),
    )
    categories: Optional[list[str]] = Field(
        default=None,
        description=(
            "Restrict deliveries to events whose action_category is "
            "in this list. Omit to receive all categories."
        ),
    )


class AuditStreamRead(BaseModel):
    """Read shape for an audit stream."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    name: str
    url: str
    signing_key_id: Optional[UUID]
    enabled: bool
    paused_at: Optional[datetime]
    pause_reason: Optional[str]
    categories: Optional[list[str]]
    created_at: datetime
    updated_at: datetime


class AuditStreamListResponse(BaseModel):
    data: list[AuditStreamRead]

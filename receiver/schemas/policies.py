"""Pydantic schemas for /v1/policies endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


VALID_ACTIONS = {"log", "alert", "block", "steer", "throttle", "allow", "require_approval"}

# Scope values accepted in a throttle action_config. Determines what the
# rate-limit counter is keyed by:
#   * "agent"  — per (policy, agent_id) — the most common semantic
#   * "global" — per (policy) — one shared bucket across all agents
VALID_THROTTLE_SCOPES = {"agent", "global"}


def validate_action_config(action: str, action_config: dict[str, Any]) -> None:
    """Validate ``action_config`` for actions that require structured config.

    Raises ``ValueError`` with a 400-ready message on malformed input.
    ``log``, ``alert``, ``block``, ``steer`` accept any shape today (steer
    looks for ``replacement``/``message`` keys but tolerates their absence
    by falling back to a generic string).

    ``throttle`` requires explicit shape:

        {
            "max_calls":      int   > 0,    # bucket capacity / burst
            "window_seconds": int|float > 0, # interval the burst refills over
            "scope":          "agent" | "global"  (optional, default "agent")
        }

    Validating server-side gives operators an immediate 400 on a malformed
    rule rather than silent failure at SDK enforcement time.
    """
    if action != "throttle":
        return

    max_calls = action_config.get("max_calls")
    if not isinstance(max_calls, int) or isinstance(max_calls, bool) or max_calls <= 0:
        raise ValueError(
            "throttle action_config.max_calls must be a positive integer, "
            f"got {max_calls!r}"
        )

    window_seconds = action_config.get("window_seconds")
    if (
        not isinstance(window_seconds, (int, float))
        or isinstance(window_seconds, bool)
        or window_seconds <= 0
    ):
        raise ValueError(
            "throttle action_config.window_seconds must be a positive number, "
            f"got {window_seconds!r}"
        )

    scope = action_config.get("scope", "agent")
    if scope not in VALID_THROTTLE_SCOPES:
        raise ValueError(
            f"throttle action_config.scope must be one of "
            f"{sorted(VALID_THROTTLE_SCOPES)}, got {scope!r}"
        )


class PolicyCreate(BaseModel):
    """POST /v1/policies request body.

    Field validation:
    - name and match_expression must be non-empty
    - action must be one of the five valid values
    - applies_to defaults to empty list (= all spans)
    - action_config defaults to empty dict
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    match_expression: str = Field(min_length=1)
    action: str = Field(pattern="^(log|alert|block|steer|throttle|allow|require_approval)$")
    description: Optional[str] = None
    action_config: dict[str, Any] = Field(default_factory=dict)
    applies_to: list[str] = Field(default_factory=list)
    enabled: bool = True
    priority: int = 0
    shadow: bool = False


class PolicyUpdate(BaseModel):
    """PATCH /v1/policies/{id} request body. All fields optional."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    match_expression: Optional[str] = Field(default=None, min_length=1)
    action: Optional[str] = Field(
        default=None, pattern="^(log|alert|block|steer|throttle|allow|require_approval)$",
    )
    description: Optional[str] = None
    action_config: Optional[dict[str, Any]] = None
    applies_to: Optional[list[str]] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None


class PolicyRead(BaseModel):
    """Response model for GET /v1/policies (list) and GET /v1/policies/{id}."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    name: str
    description: Optional[str] = None
    match_expression: str
    action: str
    action_config: dict[str, Any] = Field(default_factory=dict)
    applies_to: list[str] = Field(default_factory=list)
    enabled: bool
    priority: int
    match_count: int = 0
    last_matched_at: Optional[datetime] = None
    shadow: bool = False
    created_at: datetime
    updated_at: datetime

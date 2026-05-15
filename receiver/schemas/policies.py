"""Pydantic schemas for /v1/policies endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


VALID_ACTIONS = {"log", "alert", "block", "steer"}


class PolicyCreate(BaseModel):
    """POST /v1/policies request body.

    Field validation:
    - name and match_expression must be non-empty
    - action must be one of the four valid values
    - applies_to defaults to empty list (= all spans)
    - action_config defaults to empty dict
    """

    name: str = Field(min_length=1, max_length=200)
    match_expression: str = Field(min_length=1)
    action: str = Field(pattern="^(log|alert|block|steer)$")
    description: Optional[str] = None
    action_config: dict[str, Any] = Field(default_factory=dict)
    applies_to: list[str] = Field(default_factory=list)
    enabled: bool = True
    priority: int = 0


class PolicyUpdate(BaseModel):
    """PATCH /v1/policies/{id} request body. All fields optional."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    match_expression: Optional[str] = Field(default=None, min_length=1)
    action: Optional[str] = Field(default=None, pattern="^(log|alert|block|steer)$")
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
    created_at: datetime
    updated_at: datetime

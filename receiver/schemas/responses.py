"""Shared response schemas for OpenAPI spec quality.

These models ensure the auto-generated OpenAPI spec has accurate,
typed response schemas that enterprises can use to auto-generate
client SDKs. Without them, most endpoints show "any object" in
the spec.

Research: FastAPI response model best practices (tiangolo docs,
zhanymkanov/fastapi-best-practices). Key principle: separate
Pydantic models for request and response, use response_model on
decorator for validation + filtering + OpenAPI schema.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ---- Standard error --------------------------------------------------------

class ErrorDetail(BaseModel):
    """Standard error response. All endpoints should return this shape
    for 4xx/5xx responses."""
    code: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable error message")
    details: Optional[list[dict[str, Any]]] = Field(
        default=None, description="Additional error context"
    )


class ErrorResponse(BaseModel):
    error: ErrorDetail


# ---- Paginated list wrapper ------------------------------------------------

class PaginatedResponse(BaseModel):
    """Base for paginated list responses."""
    next_cursor: Optional[str] = None


# ---- Policy responses ------------------------------------------------------

class PolicyListResponse(BaseModel):
    data: list[dict[str, Any]]


class PolicyVersionResponse(BaseModel):
    id: int
    policy_id: str
    project_id: str
    version: int
    name: str
    description: Optional[str] = None
    match_expression: str
    action: str
    action_config: dict[str, Any] = Field(default_factory=dict)
    applies_to: list[str] = Field(default_factory=list)
    enabled: bool = True
    priority: int = 0
    change_type: str
    changed_at: str


class PolicyVersionListResponse(BaseModel):
    data: list[PolicyVersionResponse]


class BatchResponse(BaseModel):
    action: str
    affected: int
    total: int


class ConflictItem(BaseModel):
    type: str = Field(..., description="contradiction, redundancy, or shadowing")
    policy_a: dict[str, str]
    policy_b: dict[str, str]
    reason: str


class ConflictsResponse(BaseModel):
    conflicts: list[ConflictItem]
    policies_analyzed: int


# ---- Analytics responses ---------------------------------------------------

class AggregateRow(BaseModel):
    dimension: Optional[str] = None
    bucket: Optional[int] = None
    span_count: int
    total_cost_usd: str
    total_input_tokens: int
    total_output_tokens: int


class AggregateResponse(BaseModel):
    data: list[AggregateRow]
    group_by: str
    time_bucket: Optional[str] = None


class TraceResponse(BaseModel):
    trace_id: str
    project_id: str
    start_time_unix_nano: Optional[int] = None
    end_time_unix_nano: Optional[int] = None
    agent_name: Optional[str] = None
    workflow_name: Optional[str] = None
    span_count: Optional[int] = None
    total_cost_usd: Optional[str] = None
    intervention_state: Optional[str] = None


class TraceListResponse(BaseModel):
    data: list[TraceResponse]
    next_cursor: Optional[str] = None


class SpanNode(BaseModel):
    span_id: str
    parent_span_id: Optional[str] = None
    name: Optional[str] = None
    kind: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration_ms: Optional[float] = None
    status_code: Optional[str] = None
    agent_name: Optional[str] = None
    tool_name: Optional[str] = None
    request_model: Optional[str] = None
    operation_name: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[str] = None
    intervention_state: Optional[str] = None
    children: list["SpanNode"] = Field(default_factory=list)


SpanNode.model_rebuild()


class TraceTreeResponse(BaseModel):
    trace: TraceResponse
    root: Any = None  # SpanNode or list[SpanNode]
    span_count: int


# ---- Project responses -----------------------------------------------------

class ProjectResponse(BaseModel):
    id: str
    name: str
    slug: str
    api_key: Optional[str] = None
    api_key_scopes: Optional[list[str]] = None
    created_at: Optional[str] = None
    deleted_at: Optional[str] = None
    resource_counts: Optional[dict[str, int]] = None


class ProjectListResponse(BaseModel):
    data: list[ProjectResponse]


# ---- Project settings ------------------------------------------------------

class ProjectSettingsResponse(BaseModel):
    intervention_default_action: str
    trace_retention_days: int


# ---- Auth responses --------------------------------------------------------

class AuthTokenResponse(BaseModel):
    token: str
    user: dict[str, Any]
    message: str


class UserProfileResponse(BaseModel):
    id: str
    email: str
    display_name: Optional[str] = None
    created_at: Optional[str] = None
    last_login_at: Optional[str] = None
    projects: list[dict[str, Any]]


# ---- Health responses ------------------------------------------------------

class HealthCheckDetail(BaseModel):
    status: str
    reason: Optional[str] = None
    latency_ms: Optional[float] = None
    current: Optional[str] = None
    head: Optional[str] = None
    current_partition: Optional[str] = None


class ReadinessResponse(BaseModel):
    status: str = Field(..., description="'ready' or 'not_ready'")
    checks: dict[str, HealthCheckDetail]


# ---- Template responses ----------------------------------------------------

class PolicyTemplateResponse(BaseModel):
    id: str
    name: str
    description: str
    owasp_risks: list[str]
    action: str
    match_expression: str
    action_config: dict[str, Any] = Field(default_factory=dict)
    applies_to: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class PolicyTemplateListResponse(BaseModel):
    data: list[PolicyTemplateResponse]


class PolicyTemplateApplyResponse(BaseModel):
    template_id: str
    policy: dict[str, Any]

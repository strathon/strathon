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


# ---- Halt responses -------------------------------------------------------

class HaltResponse(BaseModel):
    halt: dict[str, Any]


class HaltListResponse(BaseModel):
    halts: list[dict[str, Any]]


# ---- Budget responses ------------------------------------------------------

class BudgetResponse(BaseModel):
    budget: dict[str, Any]


class BudgetListResponse(BaseModel):
    budgets: list[dict[str, Any]]


class BudgetSpendResponse(BaseModel):
    budget_id: str
    scope: str
    scope_value: Optional[str] = None
    metric: str
    current_value: Any
    threshold: Any
    window_start: Optional[str] = None
    window_end: Optional[str] = None


class DeletedResponse(BaseModel):
    deleted: bool = True


# ---- API key responses -----------------------------------------------------

class ApiKeyListResponse(BaseModel):
    api_keys: list[dict[str, Any]]


class ApiKeyCreatedResponse(BaseModel):
    """Returned on create and rotate — includes the raw key shown once."""
    model_config = {"extra": "allow"}
    id: str
    name: str
    key_prefix: str
    key: str
    scopes: list[str]
    project_id: str
    created_at: str
    expires_at: Optional[str] = None
    rotated_from_id: Optional[str] = None


class ApiKeyUpdatedResponse(BaseModel):
    model_config = {"extra": "allow"}
    id: str
    name: str
    key_prefix: str
    scopes: list[str]
    project_id: str
    expires_at: Optional[str] = None


# ---- Webhook responses -----------------------------------------------------

class WebhookSigningKeyListResponse(BaseModel):
    webhook_signing_keys: list[dict[str, Any]]


class WebhookDeliveryListResponse(BaseModel):
    deliveries: list[dict[str, Any]]
    next_cursor: Optional[str] = None


class WebhookDeliveryResponse(BaseModel):
    delivery: dict[str, Any]


class WebhookReplayResponse(BaseModel):
    replayed: int
    deliveries: list[dict[str, Any]]


# ---- Model price responses -------------------------------------------------

class ModelPriceResponse(BaseModel):
    override: dict[str, Any]


class ModelPriceListResponse(BaseModel):
    overrides: list[dict[str, Any]]


# ---- Member responses ------------------------------------------------------

class MemberResponse(BaseModel):
    model_config = {"extra": "allow"}
    id: str
    email: str
    role: str
    project_id: str


class MemberListResponse(BaseModel):
    members: list[dict[str, Any]]
    count: int


# ---- Cost responses --------------------------------------------------------

class CostRollupItem(BaseModel):
    model_config = {"extra": "allow"}
    period_start: Optional[str] = None
    span_count: int = 0
    total_cost_usd: str = "0"
    total_input_tokens: int = 0
    total_output_tokens: int = 0


class CostResponse(BaseModel):
    group_by: str
    period: str
    costs: list[dict[str, Any]]


# ---- Topology responses ----------------------------------------------------

class TopologyNode(BaseModel):
    id: str
    type: str
    name: str
    span_count: int = 0
    error_count: int = 0


class TopologyEdge(BaseModel):
    source: str
    target: str
    call_count: int = 0
    error_count: int = 0
    avg_duration_ms: Optional[float] = None


class TopologyResponse(BaseModel):
    nodes: list[TopologyNode]
    edges: list[TopologyEdge]
    node_count: int
    edge_count: int


# ---- Intervention responses ------------------------------------------------

class InterventionCheckResponse(BaseModel):
    model_config = {"extra": "allow"}
    action: str
    policy_id: Optional[str] = None
    policy_name: Optional[str] = None
    message: Optional[str] = None


class InterventionSyncResponse(BaseModel):
    halted: bool

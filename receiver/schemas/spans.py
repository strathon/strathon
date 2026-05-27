"""Pydantic schemas for /v1/spans endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field


MAX_LIMIT: int = 1000
DEFAULT_LIMIT: int = 50


def _nano_to_iso(nano: int | None) -> str | None:
    """Convert nanosecond unix timestamp to ISO 8601 string."""
    if nano is None:
        return None
    return datetime.fromtimestamp(nano / 1e9, tz=timezone.utc).isoformat()


def _bytes_to_hex(b: bytes | None) -> str | None:
    """Convert BYTEA to hex string for JSON wire."""
    if b is None:
        return None
    return bytes(b).hex()


def _decimal_to_str(d: Decimal | None) -> str | None:
    """Convert Decimal to string so JSON doesn't lose precision."""
    if d is None:
        return None
    return str(d)


class SpanTokenUsage(BaseModel):
    """Token usage extracted from a span row."""

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_creation_tokens: Optional[int] = None


class SpanCost(BaseModel):
    """Cost fields extracted from a span row."""

    cost_usd: Optional[str] = None
    cost_cumulative_usd: Optional[str] = None
    cost_subtree_usd: Optional[str] = None


class SpanRead(BaseModel):
    """One span as returned by the search API.

    IDs are hex-encoded. Timestamps are ISO 8601. Decimals are strings
    to preserve precision across JSON.
    """

    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    project_id: str

    name: str
    kind: str
    start_time: str  # ISO 8601
    end_time: Optional[str] = None

    status_code: Optional[str] = None
    status_message: Optional[str] = None

    # gen_ai.* denormalized
    operation_name: Optional[str] = None
    provider_name: Optional[str] = None
    request_model: Optional[str] = None
    response_model: Optional[str] = None
    agent_name: Optional[str] = None
    agent_id: Optional[str] = None
    tool_name: Optional[str] = None
    workflow_name: Optional[str] = None
    conversation_id: Optional[str] = None

    tokens: SpanTokenUsage = Field(default_factory=SpanTokenUsage)
    cost: SpanCost = Field(default_factory=SpanCost)

    # strathon.agent.* denormalized
    agent_depth: Optional[int] = None
    spawn_parent_agent_id: Optional[str] = None
    spawn_reason: Optional[str] = None
    intervention_state: Optional[str] = None
    halt_reason: Optional[str] = None

    # Everything else
    attributes: dict[str, Any] = Field(default_factory=dict)


class SpanDetailRead(SpanRead):
    """Single span with events and links."""

    events: list[dict[str, Any]] = Field(default_factory=list)
    links: list[dict[str, Any]] = Field(default_factory=list)


class SpanListResponse(BaseModel):
    """Paginated list of spans."""

    data: list[SpanRead]
    next_cursor: Optional[str] = None


def row_to_span_read(row: dict[str, Any]) -> SpanRead:
    """Map a raw DB row to a SpanRead Pydantic shape."""
    return SpanRead(
        trace_id=_bytes_to_hex(row["trace_id"]) or "",
        span_id=_bytes_to_hex(row["span_id"]) or "",
        parent_span_id=_bytes_to_hex(row.get("parent_span_id")),
        project_id=str(row["project_id"]),
        name=row["name"],
        kind=row["kind"],
        start_time=_nano_to_iso(row["start_time_unix_nano"]) or "",
        end_time=_nano_to_iso(row.get("end_time_unix_nano")),
        status_code=row.get("status_code"),
        status_message=row.get("status_message"),
        operation_name=row.get("operation_name"),
        provider_name=row.get("provider_name"),
        request_model=row.get("request_model"),
        response_model=row.get("response_model"),
        agent_name=row.get("agent_name"),
        agent_id=row.get("agent_id"),
        tool_name=row.get("tool_name"),
        workflow_name=row.get("workflow_name"),
        conversation_id=row.get("conversation_id"),
        tokens=SpanTokenUsage(
            input_tokens=row.get("input_tokens"),
            output_tokens=row.get("output_tokens"),
            reasoning_tokens=row.get("reasoning_tokens"),
            cache_read_tokens=row.get("cache_read_tokens"),
            cache_creation_tokens=row.get("cache_creation_tokens"),
        ),
        cost=SpanCost(
            cost_usd=_decimal_to_str(row.get("cost_usd")),
            cost_cumulative_usd=_decimal_to_str(row.get("cost_cumulative_usd")),
            cost_subtree_usd=_decimal_to_str(row.get("cost_subtree_usd")),
        ),
        agent_depth=row.get("agent_depth"),
        spawn_parent_agent_id=row.get("spawn_parent_agent_id"),
        spawn_reason=row.get("spawn_reason"),
        intervention_state=row.get("intervention_state"),
        halt_reason=row.get("halt_reason"),
        attributes=row.get("attributes") or {},
    )

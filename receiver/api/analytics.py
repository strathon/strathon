"""Span analytics and trace tree endpoints.

  GET /v1/spans/aggregate          grouped counts, cost, tokens
  GET /v1/traces/{trace_id}/tree   full span hierarchy

Scope: traces:read

These are the APIs that enterprise integrations (Grafana, Datadog)
and the Strathon dashboard consume for operator analytics.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.analytics as analytics_repo
from database import get_db_session

from ._deps import require_scope


logger = logging.getLogger("strathon.receiver.api.analytics")


router = APIRouter(tags=["analytics"])


def _parse_timestamp(value: str | None, param_name: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1e9)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{param_name} must be nanosecond unix or ISO 8601: {exc}",
        ) from exc


@router.get("/v1/spans/aggregate")
async def aggregate_spans_endpoint(
    group_by: str = Query(
        default="request_model",
        description=(
            "Dimension to group by. Options: agent_name, tool_name, "
            "operation_name, request_model, provider_name, kind, "
            "status_code, intervention_state."
        ),
    ),
    time_bucket: Optional[str] = Query(
        default=None,
        description="Time bucket size: 1h, 6h, 1d, 7d, 30d. Omit for no time grouping.",
    ),
    start_after: Optional[str] = Query(default=None),
    start_before: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_TRACES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Aggregate span metrics grouped by a dimension.

    Returns count, total cost, and total tokens per group. When
    time_bucket is specified, results are further bucketed by time.
    """
    start_ns = _parse_timestamp(start_after, "start_after")
    end_ns = _parse_timestamp(start_before, "start_before")

    try:
        rows = await analytics_repo.aggregate_spans(
            session,
            ctx.project_id,
            group_by=group_by,
            time_bucket=time_bucket,
            start_after=start_ns,
            start_before=end_ns,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return {"data": rows, "group_by": group_by, "time_bucket": time_bucket}


@router.get("/v1/traces/{trace_id}/tree")
async def trace_tree_endpoint(
    trace_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_TRACES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Reconstruct the full span tree for a trace.

    Returns the trace metadata plus a nested tree of spans with
    parent-child relationships, timing, cost, and key attributes.
    Each span node has a ``children`` array.
    """
    tree = await analytics_repo.get_trace_tree(
        session, ctx.project_id, trace_id
    )
    if tree is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"trace {trace_id} not found",
        )
    return tree


@router.get("/v1/traces")
async def list_traces_endpoint(
    start_after: Optional[str] = Query(default=None),
    start_before: Optional[str] = Query(default=None),
    agent_name: Optional[str] = Query(default=None),
    intervention_state: Optional[str] = Query(default=None),
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_TRACES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List traces for the caller's project, newest first.

    Supports time range, agent name, and intervention state filters.
    Keyset cursor pagination.
    """
    start_ns = _parse_timestamp(start_after, "start_after")
    end_ns = _parse_timestamp(start_before, "start_before")

    try:
        return await analytics_repo.list_traces(
            session,
            ctx.project_id,
            limit=limit,
            cursor=cursor,
            start_after=start_ns,
            start_before=end_ns,
            agent_name=agent_name,
            intervention_state=intervention_state,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

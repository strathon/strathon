"""Cost attribution endpoint.

GET /v1/costs returns per-agent, per-model cost rollups from span
aggregation data. Designed for finance teams and budget dashboards.

Queries the existing spans table cost_usd column with grouping by
agent_name and/or request_model, bucketed by day or week.

Scope-protected: requires traces:read.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
from database import get_db_session

from ._deps import require_scope
from schemas.responses import CostResponse

router = APIRouter(prefix="/v1/costs", tags=["costs"])

VALID_GROUP_BY = {"agent", "model", "agent_model"}
VALID_PERIODS = {"day", "week"}


@router.get("", response_model=CostResponse)
async def get_costs(
    request: Request,
    group_by: str = "model",
    period: str = "day",
    start_after: Optional[int] = None,
    start_before: Optional[int] = None,
    limit: int = 100,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_TRACES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Per-agent, per-model cost rollups from span data.

    Parameters:
        group_by: "agent", "model", or "agent_model"
        period: "day" or "week"
        start_after: filter spans after this unix nano timestamp
        start_before: filter spans before this unix nano timestamp
        limit: max rows returned (default 100)

    Returns:
        Array of {dimension, period_start, span_count, total_cost_usd,
        total_input_tokens, total_output_tokens}.
    """
    if group_by not in VALID_GROUP_BY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"group_by must be one of {sorted(VALID_GROUP_BY)}",
        )
    if period not in VALID_PERIODS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"period must be one of {sorted(VALID_PERIODS)}",
        )

    project_id = ctx.project_id

    # Build the GROUP BY columns.
    if group_by == "agent":
        dim_col = "agent_name"
        dim_label = "agent_name"
    elif group_by == "model":
        dim_col = "request_model"
        dim_label = "model"
    else:  # agent_model
        dim_col = "agent_name || '/' || request_model"
        dim_label = "agent_model"

    # Time bucketing.
    bucket_seconds = 86400 if period == "day" else 604800
    bucket_expr = (
        f"TO_TIMESTAMP("
        f"FLOOR(start_time_unix_nano / 1000000000 / {bucket_seconds}) "
        f"* {bucket_seconds}"
        f") AT TIME ZONE 'UTC'"
    )

    # Build query.
    where_parts = ["project_id = :project_id"]
    params: dict[str, Any] = {"project_id": project_id, "limit": limit}

    if start_after is not None:
        where_parts.append("start_time_unix_nano > :start_after")
        params["start_after"] = start_after
    if start_before is not None:
        where_parts.append("start_time_unix_nano < :start_before")
        params["start_before"] = start_before

    where_clause = " AND ".join(where_parts)

    sql = (
        f"SELECT "
        f"  {dim_col} AS dimension, "
        f"  {bucket_expr} AS period_start, "
        f"  COUNT(*) AS span_count, "
        f"  COALESCE(SUM(cost_usd), 0) AS total_cost_usd, "
        f"  COALESCE(SUM(input_tokens), 0) AS total_input_tokens, "
        f"  COALESCE(SUM(output_tokens), 0) AS total_output_tokens "
        f"FROM spans "
        f"WHERE {where_clause} "
        f"GROUP BY dimension, period_start "
        f"ORDER BY period_start DESC, total_cost_usd DESC "
        f"LIMIT :limit"
    )

    result = await session.execute(text(sql), params)
    rows = result.mappings().all()

    return {
        "group_by": group_by,
        "period": period,
        "costs": [
            {
                dim_label: row["dimension"],
                "period_start": str(row["period_start"]) if row["period_start"] else None,
                "span_count": row["span_count"],
                "total_cost_usd": str(row["total_cost_usd"]),
                "total_input_tokens": row["total_input_tokens"],
                "total_output_tokens": row["total_output_tokens"],
            }
            for row in rows
        ],
    }

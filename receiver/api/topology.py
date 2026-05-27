"""Agent topology map endpoint.

GET /v1/topology returns an aggregated agent→tool graph from span data.
Response format: {nodes: [...], edges: [...]} — compatible with Grafana
Node Graph panel for direct dashboard rendering.

Scope-protected: requires traces:read.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
from database import get_db_session

from ._deps import require_scope
from schemas.responses import TopologyResponse

router = APIRouter(prefix="/v1/topology", tags=["topology"])


@router.get("", response_model=TopologyResponse)
async def get_topology(
    request: Request,
    start_after: Optional[int] = None,
    start_before: Optional[int] = None,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_TRACES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Aggregated agent→tool topology graph from span data.

    Nodes are unique agents and tools. Edges are agent→tool call pairs
    with call count, error count, and average duration.

    Parameters:
        start_after: filter spans after this unix nano timestamp
        start_before: filter spans before this unix nano timestamp
    """
    project_id = ctx.project_id
    params: dict[str, Any] = {"project_id": project_id}

    where_parts = ["project_id = :project_id"]
    if start_after is not None:
        where_parts.append("start_time_unix_nano > :start_after")
        params["start_after"] = start_after
    if start_before is not None:
        where_parts.append("start_time_unix_nano < :start_before")
        params["start_before"] = start_before

    where_clause = " AND ".join(where_parts)

    # ---- Edges: agent→tool pairs ----
    edges_sql = (
        f"SELECT "
        f"  agent_name, "
        f"  tool_name, "
        f"  COUNT(*) AS call_count, "
        f"  COUNT(*) FILTER (WHERE status_code = 'ERROR') AS error_count, "
        f"  AVG((end_time_unix_nano - start_time_unix_nano) / 1e6) "
        f"    AS avg_duration_ms "
        f"FROM spans "
        f"WHERE {where_clause} "
        f"  AND agent_name IS NOT NULL AND agent_name != '' "
        f"  AND tool_name IS NOT NULL AND tool_name != '' "
        f"GROUP BY agent_name, tool_name "
        f"ORDER BY call_count DESC "
        f"LIMIT 500"
    )
    edges_result = await session.execute(text(edges_sql), params)
    edge_rows = edges_result.mappings().all()

    # ---- Agent nodes ----
    agents_sql = (
        f"SELECT "
        f"  agent_name, "
        f"  COUNT(*) AS span_count, "
        f"  COUNT(*) FILTER (WHERE status_code = 'ERROR') AS error_count "
        f"FROM spans "
        f"WHERE {where_clause} "
        f"  AND agent_name IS NOT NULL AND agent_name != '' "
        f"GROUP BY agent_name "
        f"ORDER BY span_count DESC "
        f"LIMIT 200"
    )
    agents_result = await session.execute(text(agents_sql), params)
    agent_rows = agents_result.mappings().all()

    # ---- Tool nodes ----
    tools_sql = (
        f"SELECT "
        f"  tool_name, "
        f"  COUNT(*) AS span_count, "
        f"  COUNT(*) FILTER (WHERE status_code = 'ERROR') AS error_count "
        f"FROM spans "
        f"WHERE {where_clause} "
        f"  AND tool_name IS NOT NULL AND tool_name != '' "
        f"GROUP BY tool_name "
        f"ORDER BY span_count DESC "
        f"LIMIT 200"
    )
    tools_result = await session.execute(text(tools_sql), params)
    tool_rows = tools_result.mappings().all()

    # ---- Build response ----
    nodes = []
    for row in agent_rows:
        nodes.append({
            "id": f"agent:{row['agent_name']}",
            "type": "agent",
            "name": row["agent_name"],
            "span_count": row["span_count"],
            "error_count": row["error_count"],
        })
    for row in tool_rows:
        nodes.append({
            "id": f"tool:{row['tool_name']}",
            "type": "tool",
            "name": row["tool_name"],
            "span_count": row["span_count"],
            "error_count": row["error_count"],
        })

    edges = []
    for row in edge_rows:
        edges.append({
            "source": f"agent:{row['agent_name']}",
            "target": f"tool:{row['tool_name']}",
            "call_count": row["call_count"],
            "error_count": row["error_count"],
            "avg_duration_ms": (
                round(float(row["avg_duration_ms"]), 2)
                if row["avg_duration_ms"] is not None
                else None
            ),
        })

    return {
        "nodes": nodes,
        "edges": edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }

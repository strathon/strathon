"""Agent inventory with compliance risk scoring.

GET /v1/agents returns auto-discovered inventory of every agent from
span data, with compliance metadata and risk scoring per NIST AI RMF
GOVERN 1.6 (AI system inventory) and EU AI Act Article 9.

No migration needed: aggregation over existing spans, policies, budgets.

Scope: traces:read.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.policies as policies_repo
from database import get_db_session

from ._deps import require_scope

router = APIRouter(prefix="/v1/agents", tags=["agent-inventory"])

SENSITIVE_TOOLS = frozenset({
    "shell_exec", "eval", "exec", "os_system", "subprocess_run",
    "send_email", "send_message", "http_request", "fetch",
    "web_request", "curl", "database_query", "sql_query",
    "rm", "rmdir", "drop_table", "delete_database",
})


@router.get("")
async def list_agents(
    request: Request,
    days: int = 30,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_TRACES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Auto-discovered agent inventory with risk scoring.

    Parameters:
        days: lookback window in days (default 30).
    """
    project_id = ctx.project_id
    now = datetime.now(timezone.utc)
    lookback_ns = int((now - timedelta(days=days)).timestamp() * 1e9)

    # ---- Agent stats ----
    stats_sql = (
        "SELECT "
        "  agent_name, "
        "  COUNT(*) AS total_spans, "
        "  COALESCE(SUM(cost_usd), 0) AS total_cost_usd, "
        "  COUNT(*) FILTER (WHERE tool_name IS NOT NULL AND tool_name != '') "
        "    AS total_tool_calls, "
        "  MIN(start_time_unix_nano) AS first_seen_ns, "
        "  MAX(start_time_unix_nano) AS last_active_ns "
        "FROM spans "
        "WHERE project_id = :pid "
        "  AND start_time_unix_nano > :lookback "
        "  AND agent_name IS NOT NULL AND agent_name != '' "
        "GROUP BY agent_name "
        "ORDER BY total_spans DESC "
        "LIMIT 500"
    )
    stats_result = await session.execute(
        text(stats_sql), {"pid": project_id, "lookback": lookback_ns}
    )
    agent_rows = stats_result.mappings().all()

    # ---- Tools per agent ----
    tools_sql = (
        "SELECT agent_name, tool_name "
        "FROM spans "
        "WHERE project_id = :pid "
        "  AND start_time_unix_nano > :lookback "
        "  AND agent_name IS NOT NULL AND agent_name != '' "
        "  AND tool_name IS NOT NULL AND tool_name != '' "
        "GROUP BY agent_name, tool_name"
    )
    tools_result = await session.execute(
        text(tools_sql), {"pid": project_id, "lookback": lookback_ns}
    )
    agent_tools: dict[str, set[str]] = {}
    for row in tools_result.mappings():
        agent_tools.setdefault(row["agent_name"], set()).add(row["tool_name"])

    # ---- Models per agent ----
    models_sql = (
        "SELECT agent_name, request_model "
        "FROM spans "
        "WHERE project_id = :pid "
        "  AND start_time_unix_nano > :lookback "
        "  AND agent_name IS NOT NULL AND agent_name != '' "
        "  AND request_model IS NOT NULL AND request_model != '' "
        "GROUP BY agent_name, request_model"
    )
    models_result = await session.execute(
        text(models_sql), {"pid": project_id, "lookback": lookback_ns}
    )
    agent_models: dict[str, set[str]] = {}
    for row in models_result.mappings():
        agent_models.setdefault(row["agent_name"], set()).add(row["request_model"])

    # ---- Policy and budget coverage ----
    existing_policies = await policies_repo.list_policies(
        session, project_id, only_enabled=True
    )

    budget_sql = (
        "SELECT scope_value FROM budgets "
        "WHERE project_id = :pid AND is_active = TRUE "
        "  AND scope = 'agent'"
    )
    budget_result = await session.execute(text(budget_sql), {"pid": project_id})
    budgeted_agents = {row["scope_value"] for row in budget_result.mappings()}

    # Also check project-level budgets.
    project_budget_sql = (
        "SELECT COUNT(*) AS cnt FROM budgets "
        "WHERE project_id = :pid AND is_active = TRUE AND scope = 'project'"
    )
    pb_result = await session.execute(text(project_budget_sql), {"pid": project_id})
    has_project_budget = (pb_result.mappings().first() or {}).get("cnt", 0) > 0

    # ---- Build response ----
    agents = []
    for row in agent_rows:
        agent = row["agent_name"]
        tools = sorted(agent_tools.get(agent, set()))
        models = sorted(agent_models.get(agent, set()))

        # Count covering policies.
        covering = 0
        has_approval = False
        has_block_throttle = False
        for p in existing_policies:
            applies = p.applies_to or []
            expr_lower = p.match_expression.lower()
            if (
                not applies
                or agent in applies
                or agent.lower() in expr_lower
            ):
                covering += 1
                if p.action in ("block", "throttle"):
                    has_block_throttle = True
                if p.action == "require_approval":
                    has_approval = True

        has_budget = agent in budgeted_agents or has_project_budget
        sensitive_used = set(tools) & SENSITIVE_TOOLS

        # Risk scoring.
        risk_score, risk_factors = _compute_risk(
            agent=agent,
            tools=tools,
            sensitive_used=sensitive_used,
            covering=covering,
            has_block_throttle=has_block_throttle,
            has_approval=has_approval,
            has_budget=has_budget,
        )

        first_seen_ns = row["first_seen_ns"]
        last_active_ns = row["last_active_ns"]

        agents.append({
            "agent_name": agent,
            "first_seen": (
                datetime.fromtimestamp(first_seen_ns / 1e9, tz=timezone.utc).isoformat()
                if first_seen_ns else None
            ),
            "last_active": (
                datetime.fromtimestamp(last_active_ns / 1e9, tz=timezone.utc).isoformat()
                if last_active_ns else None
            ),
            "total_spans": row["total_spans"],
            "total_cost_usd": str(row["total_cost_usd"]),
            "total_tool_calls": row["total_tool_calls"],
            "tools_used": tools,
            "models_used": models,
            "policies_covering": covering,
            "has_budget": has_budget,
            "has_approval_policy": has_approval,
            "risk_score": risk_score,
            "risk_factors": risk_factors,
        })

    return {
        "agents": agents,
        "count": len(agents),
        "lookback_days": days,
    }


def _compute_risk(
    *,
    agent: str,
    tools: list[str],
    sensitive_used: set[str],
    covering: int,
    has_block_throttle: bool,
    has_approval: bool,
    has_budget: bool,
) -> tuple[str, list[str]]:
    """Compute risk score and factors for an agent."""
    factors: list[str] = []

    # HIGH triggers.
    if covering == 0:
        factors.append("No policies covering this agent")
    if sensitive_used and not has_block_throttle:
        factors.append(
            f"Uses sensitive tools ({', '.join(sorted(sensitive_used))}) "
            f"with no block/throttle policy"
        )

    if any("No policies" in f or "sensitive tools" in f for f in factors):
        return "high", factors

    # MEDIUM triggers.
    if sensitive_used and not has_approval:
        factors.append(
            "Uses sensitive tools but no human approval policy"
        )
    if not has_budget:
        factors.append("No cost budget configured")

    if factors:
        return "medium", factors

    # LOW: fully covered.
    factors.append("Policies, budget, and approval coverage in place")
    return "low", factors

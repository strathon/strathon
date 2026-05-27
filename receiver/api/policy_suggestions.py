"""Automated policy suggestions.

GET /v1/policies/suggest analyzes recent span data and suggests policies
the operator is missing. Each suggestion includes a risk level, OWASP ASI
reference, and a ready-to-use policy JSON.

No migration needed: read-only analysis over existing spans, policies,
and budgets data.

Scope: traces:read + policies:read.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.policies as policies_repo
from database import get_db_session

from ._deps import require_scope

router = APIRouter(prefix="/v1/policies", tags=["policy-suggestions"])

# Tools considered high-risk for OWASP ASI-02/ASI-05 if uncovered.
SENSITIVE_TOOLS = frozenset({
    "shell_exec", "eval", "exec", "os_system", "subprocess_run",
    "rm", "rmdir", "drop_table", "delete_database", "format_disk",
    "send_email", "send_message", "http_request", "fetch",
    "web_request", "curl", "database_query", "sql_query",
})

# Tools that warrant require_approval for ASI-09.
APPROVAL_WORTHY_TOOLS = frozenset({
    "send_email", "send_message", "http_request", "database_query",
    "sql_query", "shell_exec", "eval", "exec",
})


@router.get("/suggest")
async def suggest_policies(
    request: Request,
    days: int = 7,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_POLICIES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Analyze recent span data and suggest missing policies.

    Parameters:
        days: lookback window in days (default 7).

    Returns:
        Array of suggestions, each with risk_level, owasp_ref,
        description, and a ready-to-use policy JSON.
    """
    project_id = ctx.project_id
    lookback_ns = int(
        (datetime.now(timezone.utc).timestamp() - days * 86400) * 1e9
    )

    # ---- Gather data ----
    agent_tools = await _get_agent_tool_usage(session, project_id, lookback_ns)
    agent_stats = await _get_agent_stats(session, project_id, lookback_ns)
    existing_policies = await policies_repo.list_policies(
        session, project_id, only_enabled=True
    )
    existing_budgets = await _get_budget_count(session, project_id)

    # Index existing policy coverage.
    policy_actions = set()
    policy_tool_refs = set()
    has_approval_policy = False
    for p in existing_policies:
        policy_actions.add(p.action)
        expr = p.match_expression.lower()
        for tool in SENSITIVE_TOOLS:
            if tool in expr:
                policy_tool_refs.add(tool)
        if p.action == "require_approval":
            has_approval_policy = True

    # ---- Generate suggestions ----
    suggestions: list[dict[str, Any]] = []

    # 1. Agents using sensitive tools with no block policy.
    for agent, tools in agent_tools.items():
        sensitive_used = tools & SENSITIVE_TOOLS
        uncovered = sensitive_used - policy_tool_refs
        if uncovered:
            tool_list = sorted(uncovered)
            suggestions.append({
                "risk_level": "high",
                "owasp_ref": "ASI-02 (Tool Misuse), ASI-05 (Code Execution)",
                "description": (
                    f"Agent '{agent}' uses sensitive tools "
                    f"({', '.join(tool_list)}) with no blocking policy."
                ),
                "recommendation": "Add a block policy for these tools.",
                "policy": {
                    "name": f"block-sensitive-tools-{agent}",
                    "match_expression": (
                        'attrs["gen_ai.tool.name"] in '
                        + str(tool_list).replace("'", '"')
                    ),
                    "action": "block",
                    "applies_to": [agent],
                    "description": (
                        f"Auto-suggested: block sensitive tools for {agent}"
                    ),
                },
            })

    # 2. Agents with no policies at all.
    for agent in agent_stats:
        agent_has_policy = any(
            agent.lower() in (p.match_expression.lower() + " ".join(p.applies_to).lower())
            for p in existing_policies
        )
        if not agent_has_policy and not existing_policies:
            suggestions.append({
                "risk_level": "high",
                "owasp_ref": "ASI-02 (Tool Misuse)",
                "description": (
                    f"Agent '{agent}' has zero policy coverage."
                ),
                "recommendation": (
                    "Add at least one policy. Consider deny-by-default mode."
                ),
                "policy": {
                    "name": f"log-all-{agent}",
                    "match_expression": "true",
                    "action": "log",
                    "applies_to": [agent],
                    "description": (
                        f"Auto-suggested: baseline logging for {agent}"
                    ),
                },
            })

    # 3. High call rate with no throttle policy.
    for agent, stats in agent_stats.items():
        calls_per_hour = stats.get("calls_per_hour", 0)
        has_throttle = "throttle" in policy_actions
        if calls_per_hour > 100 and not has_throttle:
            suggestions.append({
                "risk_level": "medium",
                "owasp_ref": "ASI-04 (Excessive Agency)",
                "description": (
                    f"Agent '{agent}' averages {calls_per_hour:.0f} tool "
                    f"calls/hour with no throttle policy."
                ),
                "recommendation": "Add a throttle policy to limit call rate.",
                "policy": {
                    "name": f"throttle-{agent}",
                    "match_expression": "true",
                    "action": "throttle",
                    "action_config": {
                        "max_calls": 100,
                        "window_seconds": 3600,
                        "scope": "agent",
                    },
                    "applies_to": [agent],
                    "description": (
                        f"Auto-suggested: rate-limit {agent} to 100 calls/hour"
                    ),
                },
            })

    # 4. No budget configured.
    if existing_budgets == 0 and agent_stats:
        total_cost = sum(
            s.get("total_cost_usd", 0) for s in agent_stats.values()
        )
        if total_cost > 0:
            suggestions.append({
                "risk_level": "medium",
                "owasp_ref": "ASI-04 (Excessive Agency)",
                "description": (
                    f"No cost budgets configured. "
                    f"${total_cost:.2f} spent in the last {days} days."
                ),
                "recommendation": (
                    "Create a cost budget via POST /v1/budgets."
                ),
                "policy": None,  # Budget, not a policy.
            })

    # 5. No human approval policy on sensitive tools.
    if not has_approval_policy:
        any_sensitive = any(
            tools & APPROVAL_WORTHY_TOOLS
            for tools in agent_tools.values()
        )
        if any_sensitive:
            suggestions.append({
                "risk_level": "medium",
                "owasp_ref": (
                    "ASI-09 (Over-trust in Agents), "
                    "EU AI Act Art 14 (Human Oversight)"
                ),
                "description": (
                    "Agents use sensitive tools but no require_approval "
                    "policy is configured."
                ),
                "recommendation": (
                    "Add a require_approval policy for sensitive tool calls."
                ),
                "policy": {
                    "name": "require-approval-sensitive-tools",
                    "match_expression": (
                        'attrs["gen_ai.tool.name"] in '
                        '["send_email", "send_message", "http_request", '
                        '"database_query", "shell_exec"]'
                    ),
                    "action": "require_approval",
                    "action_config": {"timeout_seconds": 300},
                    "description": (
                        "Auto-suggested: require human approval for "
                        "sensitive tool calls"
                    ),
                },
            })

    # Sort: high first, then medium, then low.
    priority = {"high": 0, "medium": 1, "low": 2}
    suggestions.sort(key=lambda s: priority.get(s["risk_level"], 9))

    return {
        "suggestions": suggestions,
        "count": len(suggestions),
        "lookback_days": days,
        "agents_analyzed": len(agent_stats),
    }


# ---- Internal query helpers ----


async def _get_agent_tool_usage(
    session: AsyncSession,
    project_id: Any,
    lookback_ns: int,
) -> dict[str, set[str]]:
    """Return {agent_name: {tool1, tool2, ...}} from recent spans."""
    sql = (
        "SELECT agent_name, tool_name "
        "FROM spans "
        "WHERE project_id = :pid "
        "  AND start_time_unix_nano > :lookback "
        "  AND agent_name IS NOT NULL AND agent_name != '' "
        "  AND tool_name IS NOT NULL AND tool_name != '' "
        "GROUP BY agent_name, tool_name"
    )
    result = await session.execute(
        text(sql), {"pid": project_id, "lookback": lookback_ns}
    )
    mapping: dict[str, set[str]] = {}
    for row in result.mappings():
        agent = row["agent_name"]
        tool = row["tool_name"]
        mapping.setdefault(agent, set()).add(tool)
    return mapping


async def _get_agent_stats(
    session: AsyncSession,
    project_id: Any,
    lookback_ns: int,
) -> dict[str, dict[str, Any]]:
    """Return per-agent stats from recent spans."""
    sql = (
        "SELECT "
        "  agent_name, "
        "  COUNT(*) AS total_spans, "
        "  COALESCE(SUM(cost_usd), 0) AS total_cost_usd, "
        "  COUNT(*) FILTER (WHERE tool_name IS NOT NULL AND tool_name != '') "
        "    AS tool_call_count "
        "FROM spans "
        "WHERE project_id = :pid "
        "  AND start_time_unix_nano > :lookback "
        "  AND agent_name IS NOT NULL AND agent_name != '' "
        "GROUP BY agent_name"
    )
    result = await session.execute(
        text(sql), {"pid": project_id, "lookback": lookback_ns}
    )
    stats: dict[str, dict[str, Any]] = {}
    for row in result.mappings():
        agent = row["agent_name"]
        total_spans = row["total_spans"]
        # Estimate calls per hour from the lookback window.
        # lookback_ns is the cutoff timestamp; the window is
        # (now_ns - lookback_ns). Convert to hours.
        now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
        window_hours = max((now_ns - lookback_ns) / 3.6e12, 1)
        stats[agent] = {
            "total_spans": total_spans,
            "total_cost_usd": float(row["total_cost_usd"]),
            "tool_call_count": row["tool_call_count"],
            "calls_per_hour": row["tool_call_count"] / window_hours,
        }
    return stats


async def _get_budget_count(
    session: AsyncSession,
    project_id: Any,
) -> int:
    """Count active budgets for the project."""
    sql = (
        "SELECT COUNT(*) AS cnt FROM budgets "
        "WHERE project_id = :pid AND is_active = TRUE"
    )
    result = await session.execute(text(sql), {"pid": project_id})
    row = result.mappings().first()
    return row["cnt"] if row else 0

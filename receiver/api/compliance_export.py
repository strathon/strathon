"""EU AI Act compliance evidence export.

POST /v1/compliance/export generates a structured JSON package for
conformity assessment, mapped to EU AI Act Articles 9-15 and 19.

No migration needed: compiles data from existing endpoints.

Scope: audit:read.
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

router = APIRouter(prefix="/v1/compliance", tags=["compliance"])

# Minimum retention required by EU AI Act Article 19(1).
MIN_RETENTION_DAYS = 180

# OWASP ASI risks and their IDs.
OWASP_ASI_RISKS = [
    "ASI-01", "ASI-02", "ASI-03", "ASI-04", "ASI-05",
    "ASI-06", "ASI-07", "ASI-08", "ASI-09", "ASI-10",
]


@router.post("/export")
async def export_compliance(
    request: Request,
    body: dict[str, Any] | None = None,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_AUDIT_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Generate structured EU AI Act compliance evidence package.

    Returns per-article compliance data with recommendations for gaps.
    """
    project_id = ctx.project_id
    framework = (body or {}).get("framework", "eu_ai_act")
    now = datetime.now(timezone.utc)

    # ---- Gather data ----
    policies = await policies_repo.list_policies(session, project_id)
    enabled_policies = [p for p in policies if p.enabled]
    agent_count = await _count_distinct(
        session, project_id, "agent_name", days=30
    )
    tool_count = await _count_distinct(
        session, project_id, "tool_name", days=30
    )
    audit_stats = await _audit_stats(session, project_id)
    approval_stats = await _approval_stats(session, project_id)
    halt_count = await _halt_count(session, project_id)
    retention_days = await _get_retention_days(session, project_id)
    budget_count = await _budget_count(session, project_id)
    owasp_coverage = _compute_owasp_coverage(enabled_policies)

    # ---- Build article sections ----
    recommendations: list[str] = []

    # Article 9: Risk Management System
    article_9 = {
        "description": "Risk management system (continuous, lifecycle-spanning)",
        "agents_discovered": agent_count,
        "tools_discovered": tool_count,
        "active_policies": len(enabled_policies),
        "policy_actions_used": sorted({p.action for p in enabled_policies}),
        "owasp_risks_covered": owasp_coverage["covered"],
        "owasp_risks_uncovered": owasp_coverage["uncovered"],
        "compliant": len(enabled_policies) > 0 and agent_count > 0,
    }
    if not enabled_policies:
        recommendations.append(
            "No active policies configured. Article 9 requires a risk "
            "management system with risk identification and mitigation."
        )
    if owasp_coverage["uncovered"]:
        recommendations.append(
            f"OWASP ASI risks without policy coverage: "
            f"{', '.join(owasp_coverage['uncovered'])}. "
            f"Consider adding policy templates for these risks."
        )

    # Article 11: Technical Documentation (Annex IV)
    article_11 = {
        "description": "Technical documentation (Annex IV, 9 sections)",
        "system_description": {
            "framework": "Strathon AI Agent Firewall",
            "framework_integrations": 10,
            "policy_engine": "CEL (Common Expression Language)",
            "enforcement_actions": [
                "block", "steer", "throttle", "log", "alert",
                "allow", "require_approval",
            ],
            "audit_mechanism": "HMAC-SHA256 hash chain with Merkle anchors",
        },
        "policies_exported": len(policies),
        "compliant": True,
    }

    # Article 12: Record-Keeping (automatic event logging)
    article_12 = {
        "description": (
            "Automatic event logging over the system lifetime "
            "(Article 12(1))"
        ),
        "audit_log": {
            "total_events": audit_stats.get("total_events", 0),
            "hash_chain_type": "HMAC-SHA256",
            "latest_anchor": audit_stats.get("latest_anchor"),
            "anchors_count": audit_stats.get("anchors_count", 0),
        },
        "span_logging": {
            "protocol": "OTLP protobuf",
            "automatic": True,
            "frameworks_instrumented": 10,
        },
        "compliant": audit_stats.get("total_events", 0) > 0,
    }
    if audit_stats.get("total_events", 0) == 0:
        recommendations.append(
            "No audit events recorded. Article 12 requires automatic "
            "event logging integrated into the system."
        )

    # Article 14: Human Oversight
    article_14 = {
        "description": (
            "Human oversight capability "
            "(human-in-the-loop, on-the-loop, in-command)"
        ),
        "human_in_the_loop": {
            "mechanism": "Kill-switch halts (POST /v1/halts)",
            "halts_issued": halt_count,
        },
        "human_on_the_loop": {
            "mechanism": "Continuous policy enforcement + alert webhooks",
            "active_policies": len(enabled_policies),
        },
        "human_in_command": {
            "mechanism": (
                "Deny-by-default mode + require_approval action"
            ),
            "approval_workflow": {
                "total_approvals": approval_stats.get("total", 0),
                "approved": approval_stats.get("approved", 0),
                "denied": approval_stats.get("denied", 0),
                "expired": approval_stats.get("expired", 0),
                "pending": approval_stats.get("pending", 0),
                "approval_rate": approval_stats.get("approval_rate"),
            },
        },
        "compliant": halt_count >= 0,  # Capability exists even if unused.
    }
    has_approval_policy = any(
        p.action == "require_approval" for p in enabled_policies
    )
    if not has_approval_policy:
        recommendations.append(
            "No require_approval policies configured. Article 14 requires "
            "human oversight capability for high-risk decisions."
        )

    # Article 15: Accuracy, Robustness and Cybersecurity
    article_15 = {
        "description": "Accuracy, robustness and cybersecurity",
        "active_policies": len(enabled_policies),
        "owasp_coverage_count": len(owasp_coverage["covered"]),
        "budget_enforcement": budget_count > 0,
        "rate_limiting": any(
            p.action == "throttle" for p in enabled_policies
        ),
        "auth_mechanism": "Argon2id + SHA-256 API keys + HMAC webhooks",
        "key_rotation": "Supported (grace period)",
        "compliant": len(enabled_policies) > 0,
    }

    # Article 19: Retention (minimum 6 months / 180 days)
    retention_compliant = retention_days >= MIN_RETENTION_DAYS
    article_19 = {
        "description": (
            "Log retention (minimum 6 months per Article 19(1))"
        ),
        "configured_retention_days": retention_days,
        "required_minimum_days": MIN_RETENTION_DAYS,
        "compliant": retention_compliant,
    }
    if not retention_compliant:
        recommendations.append(
            f"Retention is {retention_days} days. Article 19 requires "
            f"minimum {MIN_RETENTION_DAYS} days (6 months)."
        )

    return {
        "framework": framework,
        "generated_at": now.isoformat(),
        "project_id": str(project_id),
        "articles": {
            "article_9_risk_management": article_9,
            "article_11_technical_documentation": article_11,
            "article_12_event_logging": article_12,
            "article_14_human_oversight": article_14,
            "article_15_robustness": article_15,
            "article_19_retention": article_19,
        },
        "recommendations": recommendations,
        "recommendation_count": len(recommendations),
    }


# ---- Internal helpers ----


async def _count_distinct(
    session: AsyncSession, project_id: Any, column: str, days: int,
) -> int:
    lookback = int(
        (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1e9
    )
    sql = (
        f"SELECT COUNT(DISTINCT {column}) AS cnt FROM spans "
        f"WHERE project_id = :pid AND start_time_unix_nano > :lb "
        f"AND {column} IS NOT NULL AND {column} != ''"
    )
    result = await session.execute(
        text(sql), {"pid": project_id, "lb": lookback}
    )
    row = result.mappings().first()
    return row["cnt"] if row else 0


async def _audit_stats(session: AsyncSession, project_id: Any) -> dict:
    sql = (
        "SELECT COUNT(*) AS total FROM audit.events "
        "WHERE project_id = :pid"
    )
    result = await session.execute(text(sql), {"pid": project_id})
    total = (result.mappings().first() or {}).get("total", 0)

    anchors_sql = (
        "SELECT COUNT(*) AS cnt FROM audit.anchors"
    )
    anchors_result = await session.execute(text(anchors_sql))
    anchors = (anchors_result.mappings().first() or {}).get("cnt", 0)

    return {
        "total_events": total,
        "anchors_count": anchors,
        "latest_anchor": None,  # Could query, but not critical for export.
    }


async def _approval_stats(session: AsyncSession, project_id: Any) -> dict:
    sql = (
        "SELECT status, COUNT(*) AS cnt FROM approvals "
        "WHERE project_id = :pid GROUP BY status"
    )
    result = await session.execute(text(sql), {"pid": project_id})
    counts = {row["status"]: row["cnt"] for row in result.mappings()}
    total = sum(counts.values())
    approved = counts.get("approved", 0)
    return {
        "total": total,
        "approved": approved,
        "denied": counts.get("denied", 0),
        "expired": counts.get("expired", 0),
        "pending": counts.get("pending", 0),
        "approval_rate": (
            round(approved / total * 100, 1) if total > 0 else None
        ),
    }


async def _halt_count(session: AsyncSession, project_id: Any) -> int:
    sql = "SELECT COUNT(*) AS cnt FROM halt_state WHERE project_id = :pid"
    result = await session.execute(text(sql), {"pid": project_id})
    return (result.mappings().first() or {}).get("cnt", 0)


async def _get_retention_days(session: AsyncSession, project_id: Any) -> int:
    sql = (
        "SELECT trace_retention_days FROM project_settings "
        "WHERE project_id = :pid"
    )
    result = await session.execute(text(sql), {"pid": project_id})
    row = result.mappings().first()
    return row["trace_retention_days"] if row else 90  # default


async def _budget_count(session: AsyncSession, project_id: Any) -> int:
    sql = (
        "SELECT COUNT(*) AS cnt FROM budgets "
        "WHERE project_id = :pid AND is_active = TRUE"
    )
    result = await session.execute(text(sql), {"pid": project_id})
    return (result.mappings().first() or {}).get("cnt", 0)


def _compute_owasp_coverage(policies: list) -> dict[str, list[str]]:
    """Check which OWASP ASI risks have at least one covering policy."""
    # Map actions to ASI risks they cover.
    action_to_asi = {
        "block": {"ASI-02", "ASI-05", "ASI-08"},
        "steer": {"ASI-03"},
        "throttle": {"ASI-04"},
        "log": {"ASI-07"},
        "alert": {"ASI-07"},
        "require_approval": {"ASI-09"},
    }
    covered = set()
    for p in policies:
        covered |= action_to_asi.get(p.action, set())

    # ASI-07 is always covered (built-in logging).
    covered.add("ASI-07")

    all_risks = set(OWASP_ASI_RISKS)
    uncovered = sorted(all_risks - covered)
    return {
        "covered": sorted(covered),
        "uncovered": uncovered,
    }

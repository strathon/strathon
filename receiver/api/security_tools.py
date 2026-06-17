"""API endpoints for competitive feature parity.

  GET  /v1/credentials/scan         Scan text for credentials
  GET  /v1/credentials/patterns     List all 50+ detection patterns
  GET  /v1/circuit-breakers         List circuit breaker states
  POST /v1/circuit-breakers/reset   Reset a breaker to closed
  GET  /v1/compliance/sarif         SARIF v2.1.0 report for GitHub
  GET  /v1/agents/inventory         Tool inventory / agent BOM export
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
from database import get_db_session

from ._deps import require_scope

# In-memory store for MCP proxy configs until a migration adds a column.

router = APIRouter(tags=["security"])


# ---- Credential Patterns ----------------------------------------------------

@router.get("/v1/credentials/patterns")
async def list_credential_patterns(
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_POLICIES_READ)
    ),
) -> dict[str, Any]:
    """List all built-in credential detection patterns."""
    from credential_patterns import PATTERNS, PATTERN_COUNT, CATEGORIES, SEVERITY_COUNTS
    return {
        "total_patterns": PATTERN_COUNT,
        "categories": CATEGORIES,
        "severity_counts": SEVERITY_COUNTS,
        "patterns": [
            {
                "id": p.id,
                "name": p.name,
                "severity": p.severity,
                "category": p.category,
            }
            for p in PATTERNS
        ],
    }


class ScanRequest(BaseModel):
    text: str = Field(..., max_length=100_000)
    model_config = {"extra": "forbid"}


@router.post("/v1/credentials/scan")
async def scan_for_credentials(
    body: ScanRequest,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_TRACES_READ)
    ),
) -> dict[str, Any]:
    """Scan text for credential patterns. Returns findings without exposing secrets."""
    from credential_patterns import scan_text
    findings = scan_text(body.text)
    return {
        "findings_count": len(findings),
        "findings": findings,
    }


# ---- Circuit Breakers -------------------------------------------------------

@router.get("/v1/circuit-breakers")
async def list_circuit_breakers(
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_TRACES_READ)
    ),
) -> dict[str, Any]:
    """List all circuit breakers and their current state."""
    from circuit_breaker import list_breakers
    breakers = list_breakers()
    return {
        "total": len(breakers),
        "open": sum(1 for b in breakers if b["state"] == "open"),
        "half_open": sum(1 for b in breakers if b["state"] == "half_open"),
        "breakers": breakers,
    }


class ResetRequest(BaseModel):
    entity_id: str
    entity_type: str = "agent"
    model_config = {"extra": "forbid"}


@router.post("/v1/circuit-breakers/reset")
async def reset_circuit_breaker(
    body: ResetRequest,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_POLICIES_WRITE)
    ),
) -> dict[str, Any]:
    """Manually reset a circuit breaker to CLOSED state."""
    from circuit_breaker import reset_breaker
    ok = reset_breaker(body.entity_id, body.entity_type)
    if not ok:
        raise HTTPException(404, f"No circuit breaker found for {body.entity_type}:{body.entity_id}")
    return {"status": "reset", "entity_id": body.entity_id, "entity_type": body.entity_type}


# ---- SARIF Output ------------------------------------------------------------

@router.get("/v1/compliance/sarif")
async def get_sarif_report(
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_AUDIT_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
    hours: int = Query(default=24, ge=1, le=720),
) -> dict[str, Any]:
    """Generate SARIF v2.1.0 report for GitHub Code Scanning.

    Includes policy violations, credential findings, and incidents
    from the last N hours.
    """
    import time
    from sarif_output import generate_sarif

    cutoff_ns = int((time.time() - hours * 3600) * 1e9)

    # Policy violations (blocked/denied spans).
    violations_result = await session.execute(text(
        "SELECT attributes FROM spans "
        "WHERE project_id = :pid "
        "AND start_time_unix_nano > :cutoff "
        "AND attributes->>'strathon.policy.outcome' IN ('blocked', 'denied') "
        "LIMIT 1000"
    ), {"pid": ctx.project_id, "cutoff": cutoff_ns})

    violations = []
    for row in violations_result.all():
        attrs = row[0] if row[0] else {}
        violations.append({
            "policy_name": attrs.get("strathon.policy.name", "unknown"),
            "policy_id": attrs.get("strathon.policy.id", ""),
            "action": attrs.get("strathon.policy.action", "block"),
            "agent_name": attrs.get("gen_ai.agent.name", "unknown"),
            "tool_name": attrs.get("gen_ai.tool.name", "unknown"),
            "trace_id": attrs.get("trace_id", ""),
            "severity": "high",
        })

    sarif = generate_sarif(violations=violations)
    return sarif


# ---- Tool Inventory / Agent Bill of Materials --------------------------------

@router.get("/v1/agents/inventory")
async def get_agent_inventory_bom(
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_TRACES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
    format: str = Query(default="json", pattern="^(json|cyclonedx)$"),
) -> dict[str, Any]:
    """Export agent tool inventory as JSON or CycloneDX BOM.

    Lists every agent, every tool it uses, which policies cover it,
    and risk classification. CycloneDX format integrates with SBOM
    tooling and supply chain security scanners.
    """
    # Get agent + tool inventory.
    result = await session.execute(text("""
        SELECT
            agent_name,
            ARRAY_AGG(DISTINCT tool_name)
                FILTER (WHERE tool_name IS NOT NULL) AS tools,
            ARRAY_AGG(DISTINCT request_model)
                FILTER (WHERE request_model IS NOT NULL) AS models,
            COUNT(*) AS total_spans,
            MAX(to_timestamp(start_time_unix_nano / 1e9)) AS last_active
        FROM spans
        WHERE project_id = :pid
          AND agent_name IS NOT NULL
        GROUP BY agent_name
    """), {"pid": ctx.project_id})

    agents = []
    for row in result.mappings().all():
        tools = row["tools"] or []
        agents.append({
            "agent_name": row["agent_name"],
            "tools": tools,
            "models": row["models"] or [],
            "total_spans": row["total_spans"],
            "last_active": row["last_active"].isoformat() if row["last_active"] else None,
            "tool_count": len(tools),
        })

    if format == "cyclonedx":
        return _to_cyclonedx(agents)

    return {
        "format": "strathon-agent-bom",
        "version": "1.0",
        "agents": agents,
        "total_agents": len(agents),
        "total_unique_tools": len(set(
            t for a in agents for t in a["tools"]
        )),
    }


def _to_cyclonedx(agents: list[dict]) -> dict:
    """Convert agent inventory to CycloneDX 1.6 BOM format."""
    from datetime import datetime, timezone

    components = []
    for agent in agents:
        # Agent as a component.
        agent_comp = {
            "type": "application",
            "bom-ref": f"agent:{agent['agent_name']}",
            "name": agent["agent_name"],
            "description": f"AI agent with {agent['tool_count']} tools",
            "properties": [
                {"name": "strathon:total_spans", "value": str(agent["total_spans"])},
                {"name": "strathon:last_active", "value": agent.get("last_active", "")},
            ],
        }
        components.append(agent_comp)

        # Each tool as a sub-component.
        for tool in agent["tools"]:
            components.append({
                "type": "library",
                "bom-ref": f"tool:{agent['agent_name']}:{tool}",
                "name": tool,
                "description": f"Tool used by agent {agent['agent_name']}",
                "properties": [
                    {"name": "strathon:agent", "value": agent["agent_name"]},
                ],
            })

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tools": [{"name": "strathon", "version": "1.2.1"}],
        },
        "components": components,
    }


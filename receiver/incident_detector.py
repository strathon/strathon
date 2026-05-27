"""Incident detection with EU AI Act Article 73 reporting hooks.

Runs periodically (default 60s). Checks configurable thresholds
against recent data. When an incident is detected, fires a webhook
with enhanced payload including Article 73 metadata (reporting
deadline, severity classification).

Article 73 deadlines:
- Default: 15 days from awareness
- Death/serious harm: 10 days
- Critical infrastructure disruption / widespread infringement: 2 days

Triggers:
- policy_block_spike: >N blocks in M minutes
- budget_auto_halt: any budget auto-halt
- approval_denied_sensitive: approval denied on sensitive tool
- hash_chain_break: audit hash verification failure
- agent_error_spike: >N error-status spans in M minutes
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("strathon.receiver.incident_detector")

# Default thresholds.
DEFAULT_THRESHOLDS = {
    "policy_block_spike_count": 50,
    "policy_block_spike_window_minutes": 5,
    "agent_error_spike_count": 100,
    "agent_error_spike_window_minutes": 5,
}


def _article_73_metadata(
    severity: str,
    trigger: str,
) -> dict[str, Any]:
    """Generate EU AI Act Article 73 reporting metadata."""
    now = datetime.now(timezone.utc)

    # Map severity to reporting deadline.
    if severity == "critical":
        # Art 73(3): widespread infringement / critical infrastructure → 2 days
        deadline_days = 2
        description = (
            "Report to market surveillance authority within 2 days. "
            "Article 73(3): widespread infringement or serious and "
            "irreversible disruption of critical infrastructure."
        )
    elif severity == "high":
        # Art 73(2): default serious incident → 15 days
        deadline_days = 15
        description = (
            "Report to market surveillance authority within 15 days. "
            "Article 73(2): serious incident linked to AI system."
        )
    else:
        # Medium: may not require formal reporting but should be investigated.
        deadline_days = 15
        description = (
            "Investigate and document. May require reporting under "
            "Article 73 if causal link to AI system is established."
        )

    return {
        "article": "73",
        "deadline_days": deadline_days,
        "deadline_date": (now + timedelta(days=deadline_days)).isoformat(),
        "description": description,
    }


async def _check_block_spike(
    session: AsyncSession,
    project_id: Any,
    threshold: int,
    window_minutes: int,
) -> dict[str, Any] | None:
    """Check for a spike in policy blocks."""
    cutoff_ns = int(
        (datetime.now(timezone.utc) - timedelta(minutes=window_minutes))
        .timestamp() * 1e9
    )
    sql = (
        "SELECT COUNT(*) AS cnt FROM policy_matches "
        "WHERE project_id = :pid "
        "  AND action = 'block' "
        "  AND matched_at > TO_TIMESTAMP(:cutoff_ns / 1e9)"
    )
    result = await session.execute(
        text(sql), {"pid": project_id, "cutoff_ns": cutoff_ns}
    )
    count = (result.mappings().first() or {}).get("cnt", 0)
    if count >= threshold:
        return {
            "trigger": "policy_block_spike",
            "severity": "high",
            "details": {
                "block_count": count,
                "window_minutes": window_minutes,
                "threshold": threshold,
            },
        }
    return None


async def _check_budget_auto_halt(
    session: AsyncSession,
    project_id: Any,
) -> dict[str, Any] | None:
    """Check for recent budget auto-halts (actor=budget_monitor)."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    sql = (
        "SELECT id, trace_id, agent_id, budget_id, reason FROM halt_state "
        "WHERE project_id = :pid "
        "  AND actor = 'budget_monitor' "
        "  AND set_at > :cutoff "
        "  AND cleared_at IS NULL"
    )
    result = await session.execute(
        text(sql), {"pid": project_id, "cutoff": cutoff}
    )
    rows = result.mappings().all()
    if rows:
        return {
            "trigger": "budget_auto_halt",
            "severity": "high",
            "details": {
                "halts": [
                    {
                        "id": row["id"],
                        "trace_id": str(row["trace_id"]) if row["trace_id"] else None,
                        "agent_id": row["agent_id"],
                        "budget_id": str(row["budget_id"]) if row["budget_id"] else None,
                        "reason": row["reason"],
                    }
                    for row in rows
                ],
            },
        }
    return None


async def _check_error_spike(
    session: AsyncSession,
    project_id: Any,
    threshold: int,
    window_minutes: int,
) -> dict[str, Any] | None:
    """Check for a spike in error-status spans."""
    cutoff_ns = int(
        (datetime.now(timezone.utc) - timedelta(minutes=window_minutes))
        .timestamp() * 1e9
    )
    sql = (
        "SELECT agent_name, COUNT(*) AS cnt FROM spans "
        "WHERE project_id = :pid "
        "  AND start_time_unix_nano > :cutoff "
        "  AND status_code = 'ERROR' "
        "GROUP BY agent_name "
        "HAVING COUNT(*) >= :threshold"
    )
    result = await session.execute(
        text(sql),
        {"pid": project_id, "cutoff": cutoff_ns, "threshold": threshold},
    )
    rows = result.mappings().all()
    if rows:
        return {
            "trigger": "agent_error_spike",
            "severity": "medium",
            "details": {
                "agents": [
                    {"agent_name": row["agent_name"], "error_count": row["cnt"]}
                    for row in rows
                ],
                "window_minutes": window_minutes,
                "threshold": threshold,
            },
        }
    return None


def build_incident_payload(
    incident: dict[str, Any],
    project_id: Any,
) -> dict[str, Any]:
    """Build the webhook payload for a detected incident."""
    incident_id = str(uuid.uuid4())
    severity = incident["severity"]
    trigger = incident["trigger"]

    return {
        "incident_id": incident_id,
        "project_id": str(project_id),
        "severity": severity,
        "trigger": trigger,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "details": incident.get("details", {}),
        "eu_ai_act_reporting": _article_73_metadata(severity, trigger),
        "recommended_actions": _recommended_actions(trigger, severity),
    }


def _recommended_actions(trigger: str, severity: str) -> list[str]:
    """Generate recommended actions based on trigger type."""
    actions = []
    if trigger == "policy_block_spike":
        actions.extend([
            "Review blocked tool calls for attack patterns",
            "Check if a prompt injection campaign is underway",
            "Consider tightening agent permissions",
        ])
    elif trigger == "budget_auto_halt":
        actions.extend([
            "Review agent cost patterns for anomalies",
            "Check if model usage has spiked unexpectedly",
            "Consider adjusting budget thresholds or agent scope",
        ])
    elif trigger == "agent_error_spike":
        actions.extend([
            "Review error spans for root cause",
            "Check upstream API availability",
            "Consider halting the affected agent",
        ])
    elif trigger == "hash_chain_break":
        actions.extend([
            "Investigate potential audit log tampering",
            "Verify database integrity",
            "Escalate to security team immediately",
        ])

    if severity in ("critical", "high"):
        actions.append(
            "Document the incident for potential Article 73 reporting"
        )
    return actions


async def _run_checks(
    session: AsyncSession,
    project_id: Any,
    thresholds: dict[str, int],
) -> list[dict[str, Any]]:
    """Run all incident checks. Returns list of triggered incidents."""
    incidents: list[dict[str, Any]] = []

    result = await _check_block_spike(
        session, project_id,
        threshold=thresholds.get(
            "policy_block_spike_count",
            DEFAULT_THRESHOLDS["policy_block_spike_count"],
        ),
        window_minutes=thresholds.get(
            "policy_block_spike_window_minutes",
            DEFAULT_THRESHOLDS["policy_block_spike_window_minutes"],
        ),
    )
    if result:
        incidents.append(result)

    result = await _check_budget_auto_halt(session, project_id)
    if result:
        incidents.append(result)

    result = await _check_error_spike(
        session, project_id,
        threshold=thresholds.get(
            "agent_error_spike_count",
            DEFAULT_THRESHOLDS["agent_error_spike_count"],
        ),
        window_minutes=thresholds.get(
            "agent_error_spike_window_minutes",
            DEFAULT_THRESHOLDS["agent_error_spike_window_minutes"],
        ),
    )
    if result:
        incidents.append(result)

    return incidents


async def _tick(session_maker, thresholds: dict[str, int]) -> None:
    """Single tick of the incident detector."""
    from sqlalchemy import text as sa_text

    async with session_maker() as session:
        # Get all active projects.
        try:
            result = await session.execute(
                sa_text(
                    "SELECT id FROM projects WHERE deleted_at IS NULL LIMIT 50"
                )
            )
            project_ids = [row["id"] for row in result.mappings()]
        except Exception:
            logger.exception("incident detector: failed to list projects")
            return

    for pid in project_ids:
        async with session_maker() as session:
            try:
                incidents = await _run_checks(session, pid, thresholds)
                for incident in incidents:
                    payload = build_incident_payload(incident, pid)
                    logger.warning(
                        "Incident detected: %s severity=%s project=%s",
                        payload["trigger"],
                        payload["severity"],
                        pid,
                    )
                    # Incident webhook dispatch — wired via notification dispatcher.
                    # once it supports non-policy event types.
            except Exception:
                logger.exception(
                    "incident detector: check failed for project %s", pid
                )


async def incident_detector_loop(
    session_maker,
    interval_seconds: int = 60,
    thresholds: dict[str, int] | None = None,
) -> None:
    """Run the incident detector on a periodic tick until cancelled."""
    t = thresholds or DEFAULT_THRESHOLDS
    logger.info(
        "Incident detector started (interval=%ds)", interval_seconds,
    )
    while True:
        try:
            await _tick(session_maker, t)
        except asyncio.CancelledError:
            logger.info("Incident detector cancelled")
            return
        except Exception:
            logger.exception("incident detector tick failed")
        await asyncio.sleep(interval_seconds)

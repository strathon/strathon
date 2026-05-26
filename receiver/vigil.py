"""Behavioral drift detection via EWMA and CUSUM.

Auto-calibrates from production span data. Does nothing until
each agent has 100+ spans (configurable). Once calibrated,
fires webhook alerts when an agent's behavior deviates from
its historical baseline.

Metrics tracked per agent:
  - deny_rate: fraction of spans blocked by policies
  - tool_call_rate: tool calls per minute
  - cost_rate: USD per minute
  - error_rate: fraction of error-status spans

Algorithm:
  EWMA (Exponentially Weighted Moving Average) establishes the
  baseline. CUSUM (Cumulative Sum) detects sustained shifts away
  from that baseline. Together they catch both sudden spikes and
  gradual drift.

Research: OWASP Agentic Top 10 "excessive autonomy" detection,
Montgomery (2009) EWMA/CUSUM control charts, standard industrial
EWMA pattern.

Runs as a background task (60s tick). Configurable thresholds.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("strathon.vigil")

# ---- Configuration -----------------------------------------------------------

MIN_SPANS_FOR_BASELINE = int(os.environ.get("STRATHON_VIGIL_MIN_SPANS", "100"))
EWMA_ALPHA = float(os.environ.get("STRATHON_VIGIL_EWMA_ALPHA", "0.3"))
CUSUM_THRESHOLD = float(os.environ.get("STRATHON_VIGIL_CUSUM_THRESHOLD", "5.0"))
CUSUM_DRIFT = float(os.environ.get("STRATHON_VIGIL_CUSUM_DRIFT", "0.5"))
TICK_SECONDS = 60


# ---- Data structures ---------------------------------------------------------

@dataclass
class AgentBaseline:
    """EWMA baseline for one agent on one metric."""
    ewma: float = 0.0
    cusum_pos: float = 0.0
    cusum_neg: float = 0.0
    samples: int = 0
    calibrated: bool = False

    def update(self, value: float) -> bool:
        """Update baseline with new observation.

        Returns True if drift detected (CUSUM breach).
        """
        self.samples += 1

        if not self.calibrated:
            # Accumulating baseline.
            if self.samples == 1:
                self.ewma = value
            else:
                self.ewma = EWMA_ALPHA * value + (1 - EWMA_ALPHA) * self.ewma

            if self.samples >= MIN_SPANS_FOR_BASELINE:
                self.calibrated = True
                logger.info(
                    "Baseline calibrated after %d samples (ewma=%.4f)",
                    self.samples, self.ewma,
                )
            return False

        # Update EWMA.
        old_ewma = self.ewma
        self.ewma = EWMA_ALPHA * value + (1 - EWMA_ALPHA) * self.ewma

        # CUSUM: detect sustained shift from baseline.
        diff = value - old_ewma
        self.cusum_pos = max(0.0, self.cusum_pos + diff - CUSUM_DRIFT)
        self.cusum_neg = max(0.0, self.cusum_neg - diff - CUSUM_DRIFT)

        if self.cusum_pos > CUSUM_THRESHOLD or self.cusum_neg > CUSUM_THRESHOLD:
            # Reset CUSUM after alert to avoid repeated firing.
            self.cusum_pos = 0.0
            self.cusum_neg = 0.0
            return True

        return False


# ---- Per-agent state (in-memory, rebuilds on restart) ------------------------

_baselines: dict[str, dict[str, AgentBaseline]] = {}
# Structure: _baselines[agent_name][metric_name] = AgentBaseline


def _get_baseline(agent: str, metric: str) -> AgentBaseline:
    if agent not in _baselines:
        _baselines[agent] = {}
    if metric not in _baselines[agent]:
        _baselines[agent][metric] = AgentBaseline()
    return _baselines[agent][metric]


# ---- Tick: query recent data and update baselines ----------------------------

async def _compute_agent_metrics(
    session: AsyncSession, lookback_seconds: int = 300,
) -> list[dict[str, Any]]:
    """Query per-agent metrics for the last N seconds."""
    result = await session.execute(text("""
        SELECT
            agent_name,
            COUNT(*) AS total_spans,
            COUNT(*) FILTER (
                WHERE attributes->>'strathon.policy.outcome' IN ('blocked', 'denied')
            ) AS denied_spans,
            COUNT(*) FILTER (
                WHERE status_code = 'ERROR'
            ) AS error_spans,
            COALESCE(SUM(cost_usd), 0) AS total_cost
        FROM spans
        WHERE start_time_unix_nano > :cutoff
          AND agent_name IS NOT NULL
        GROUP BY agent_name
    """), {
        "cutoff": int((time.time() - lookback_seconds) * 1e9),
    })

    metrics = []
    for row in result.mappings().all():
        total = row["total_spans"] or 1
        metrics.append({
            "agent_name": row["agent_name"],
            "deny_rate": (row["denied_spans"] or 0) / total,
            "error_rate": (row["error_spans"] or 0) / total,
            "tool_call_rate": total / (lookback_seconds / 60),
            "cost_rate": float(row["total_cost"] or 0) / (lookback_seconds / 60),
        })
    return metrics


async def _tick(session_maker, thresholds: dict | None = None) -> list[dict]:
    """Run one vigil tick. Returns list of drift alerts."""
    alerts = []

    async with session_maker() as session:
        agent_metrics = await _compute_agent_metrics(session)

    for am in agent_metrics:
        agent = am["agent_name"]
        for metric in ("deny_rate", "error_rate", "tool_call_rate", "cost_rate"):
            baseline = _get_baseline(agent, metric)
            value = am[metric]
            drifted = baseline.update(value)

            if drifted:
                alert = {
                    "type": "behavioral_drift",
                    "agent_name": agent,
                    "metric": metric,
                    "current_value": round(value, 4),
                    "baseline_ewma": round(baseline.ewma, 4),
                    "severity": "high" if metric in ("deny_rate", "error_rate") else "medium",
                    "message": (
                        f"Agent '{agent}' {metric} drifted significantly "
                        f"(current: {value:.4f}, baseline: {baseline.ewma:.4f})"
                    ),
                }
                alerts.append(alert)
                logger.warning(
                    "Drift detected: agent=%s metric=%s value=%.4f baseline=%.4f",
                    agent, metric, value, baseline.ewma,
                )

    return alerts


# ---- Background loop ---------------------------------------------------------

async def vigil_loop(session_maker) -> None:
    """Background task that runs drift detection every TICK_SECONDS.

    Auto-calibrates per-agent baselines. Does nothing until each agent
    has MIN_SPANS_FOR_BASELINE observations. After calibration, fires
    alerts through the notification dispatcher when drift is detected.
    """
    import asyncio
    logger.info(
        "Vigil started (min_spans=%d, ewma_alpha=%.2f, cusum_threshold=%.1f)",
        MIN_SPANS_FOR_BASELINE, EWMA_ALPHA, CUSUM_THRESHOLD,
    )

    while True:
        try:
            alerts = await _tick(session_maker)
            if alerts:
                # Fire alerts through notification dispatcher.
                try:
                    from integrations.dispatcher import dispatch_event
                    async with session_maker() as session:
                        for alert in alerts:
                            # Get project_id from span data.
                            result = await session.execute(text(
                                "SELECT DISTINCT project_id FROM spans "
                                "WHERE agent_name = :agent "
                                "LIMIT 1"
                            ), {"agent": alert["agent_name"]})
                            row = result.first()
                            if row:
                                await dispatch_event(
                                    session, row[0],
                                    "behavioral_drift", alert,
                                )
                except Exception:
                    logger.exception("Failed to dispatch drift alerts")

        except asyncio.CancelledError:
            logger.info("Vigil shutting down")
            break
        except Exception:
            logger.exception("Vigil tick failed")

        await asyncio.sleep(TICK_SECONDS)

"""Cost forecasting with burn alerts.

GET /v1/costs/forecast computes burn rate from recent span costs and
projects when active budgets will be exhausted.

No migration needed: computed from existing span cost_usd data +
budget configurations.

Scope: traces:read.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
from database import get_db_session

from ._deps import require_scope

router = APIRouter(prefix="/v1/costs", tags=["cost-forecast"])


@router.get("/forecast")
async def cost_forecast(
    request: Request,
    alert_threshold_hours: int = 24,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_TRACES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Cost burn rate and budget exhaustion forecast.

    Parameters:
        alert_threshold_hours: fire budget_alert if projected exhaustion
            is within this many hours (default 24).

    Returns:
        burn_rate_usd_per_hour, projected costs, per-budget exhaustion
        forecast, and budget_alerts.
    """
    project_id = ctx.project_id
    now = datetime.now(timezone.utc)
    lookback_24h_ns = int((now - timedelta(hours=24)).timestamp() * 1e9)

    # ---- Burn rate from last 24h ----
    sql = (
        "SELECT COALESCE(SUM(cost_usd), 0) AS total_cost "
        "FROM spans "
        "WHERE project_id = :pid "
        "  AND start_time_unix_nano > :lookback"
    )
    result = await session.execute(
        text(sql), {"pid": project_id, "lookback": lookback_24h_ns}
    )
    row = result.mappings().first()
    total_24h = float(row["total_cost"]) if row else 0.0
    burn_rate = total_24h / 24.0  # USD per hour

    projected_daily = burn_rate * 24
    projected_weekly = burn_rate * 168
    projected_monthly = burn_rate * 720

    # ---- Budget exhaustion forecasts ----
    budgets_sql = (
        "SELECT id, name, scope, scope_value, max_spend_usd, budget_duration, "
        "  is_active, created_at "
        "FROM budgets "
        "WHERE project_id = :pid AND is_active = TRUE "
        "  AND max_spend_usd IS NOT NULL"
    )
    budgets_result = await session.execute(
        text(budgets_sql), {"pid": project_id}
    )
    budget_rows = budgets_result.mappings().all()

    budget_forecasts = []
    budget_alerts = []

    for b in budget_rows:
        threshold = float(b["max_spend_usd"])
        # Get current spend for this budget's window.
        duration = b["budget_duration"] or "30d"
        window_days = _parse_duration_days(duration)
        window_ns = int(
            (now - timedelta(days=window_days)).timestamp() * 1e9
        )

        spend_sql = (
            "SELECT COALESCE(SUM(cost_usd), 0) AS spent "
            "FROM spans "
            "WHERE project_id = :pid "
            "  AND start_time_unix_nano > :window_start"
        )
        spend_params: dict[str, Any] = {
            "pid": project_id,
            "window_start": window_ns,
        }
        # Scope filtering.
        if b["scope"] == "agent" and b["scope_value"]:
            spend_sql += " AND agent_name = :scope_val"
            spend_params["scope_val"] = b["scope_value"]
        elif b["scope"] == "model" and b["scope_value"]:
            spend_sql += " AND request_model = :scope_val"
            spend_params["scope_val"] = b["scope_value"]

        spend_result = await session.execute(text(spend_sql), spend_params)
        spend_row = spend_result.mappings().first()
        current_spend = float(spend_row["spent"]) if spend_row else 0.0

        remaining = max(threshold - current_spend, 0)
        if burn_rate > 0:
            hours_until_exhaustion = remaining / burn_rate
            exhaustion_date = (
                now + timedelta(hours=hours_until_exhaustion)
            ).isoformat()
        else:
            hours_until_exhaustion = None
            exhaustion_date = None

        is_alert = (
            hours_until_exhaustion is not None
            and hours_until_exhaustion <= alert_threshold_hours
        )

        forecast = {
            "budget_id": str(b["id"]),
            "budget_name": b["name"],
            "max_spend_usd": str(threshold),
            "current_spend_usd": f"{current_spend:.4f}",
            "remaining_usd": f"{remaining:.4f}",
            "hours_until_exhaustion": (
                round(hours_until_exhaustion, 1)
                if hours_until_exhaustion is not None
                else None
            ),
            "projected_exhaustion_date": exhaustion_date,
            "budget_alert": is_alert,
        }
        budget_forecasts.append(forecast)

        if is_alert:
            budget_alerts.append({
                "budget_id": str(b["id"]),
                "budget_name": b["name"],
                "severity": "warning",
                "message": (
                    f"Budget '{b['name']}' projected to exhaust in "
                    f"{hours_until_exhaustion:.1f}h at current burn rate"
                ),
            })

    return {
        "burn_rate_usd_per_hour": round(burn_rate, 6),
        "total_cost_last_24h": round(total_24h, 4),
        "projected_daily_cost": round(projected_daily, 4),
        "projected_weekly_cost": round(projected_weekly, 4),
        "projected_monthly_cost": round(projected_monthly, 4),
        "budget_forecasts": budget_forecasts,
        "budget_alerts": budget_alerts,
        "alert_threshold_hours": alert_threshold_hours,
    }


def _parse_duration_days(duration: str) -> int:
    """Parse duration string like '1d', '7d', '30d' to days."""
    d = duration.strip().lower()
    if d.endswith("d"):
        try:
            return int(d[:-1])
        except ValueError:
            pass
    return 30  # default

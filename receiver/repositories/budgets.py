"""Budget persistence and the spend-aggregation query.

The budgets table stores per-project cost/iteration limits with fixed-
window reset semantics. Each budget has:

  * scope             one of 'project' | 'agent' | 'model'
  * scope_value       agent_id, model_name, or NULL for project-scope
  * budget_duration   one of '1h' | '1d' | '7d' | '30d'
  * max_spend_usd     dollar cap for cost budgets
  * max_repeated_calls + loop_window_seconds   iteration-limit budgets
  * budget_reset_at   when this window's counter rolls over

A budget is either a COST budget (max_spend_usd is set, max_repeated_calls
is null) or an ITERATION budget (the inverse). The repository's
validate_create method enforces exactly-one.

Spend aggregation
=================

Cost budgets evaluate spend by aggregating the spans table:

    SELECT SUM(cost_usd)
    FROM spans
    WHERE project_id = ?
      AND cost_usd IS NOT NULL
      AND end_time_unix_nano >= window_start_ns
      AND (scope-specific filter)

The scope filter narrows by agent_id or request_model. The partial
index ``idx_spans_cost_window`` keeps this bounded; for a normal
workload (millions of spans, thousands of LLM spans) it runs in low
single-digit milliseconds.

Iteration budgets evaluate by counting tool spans in a rolling window:

    SELECT COUNT(*)
    FROM spans
    WHERE project_id = ?
      AND tool_name IS NOT NULL
      AND start_time_unix_nano >= (NOW() - loop_window_seconds) * 1e9

Different from cost: this is a TRUE rolling window (not fixed-window-
with-reset) because iteration limits are about runaway-loop detection,
which is inherently "in the last N seconds." A fixed window would let
a loop survive a window boundary.

Why no counter column update
============================

The earlier design had every span ingest do
``UPDATE budgets SET spent_usd = spent_usd + cost``. That serializes
every concurrent ingest on the same project on one row, which becomes
the bottleneck at scale. The aggregation approach has no contention
(spans table inserts are independent) and gives free per-trace /
per-agent / per-model rollups from the same data.

The legacy ``budgets.spent_usd`` column still exists from migration 001;
we use it as a cached snapshot the monitor writes on each tick, so a
dashboard read doesn't always pay the aggregation cost. Source of
truth is still the aggregation; the snapshot can drift by up to one
monitor-tick.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.intervention import Budget
from models.traces import Span

logger = logging.getLogger("strathon.receiver.repositories.budgets")


# ---- Constants ---------------------------------------------------------

VALID_SCOPES = {"project", "agent", "model"}
VALID_DURATIONS = {"1h", "1d", "7d", "30d"}

# Mapping from duration string to timedelta. Used by both window-start
# computation and budget_reset_at advance logic.
_DURATION_DELTA = {
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


# ---- DTO ---------------------------------------------------------------


@dataclass(frozen=True)
class BudgetRow:
    """Operator-facing view of a budget row.

    spent_usd is the CACHED value from the budgets table column. For
    fresh data, call ``compute_spend_usd``. The cache is updated on
    each monitor tick (default every 5s), so it lags reality by at
    most one tick — acceptable for dashboards, not for enforcement
    decisions (those use the live aggregation).
    """
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: Optional[str]
    scope: str                          # 'project' | 'agent' | 'model'
    scope_value: Optional[str]
    max_spend_usd: Optional[Decimal]
    spent_usd: Decimal                  # cached snapshot
    soft_limit_ratio: Optional[Decimal]
    max_repeated_calls: Optional[int]
    loop_window_seconds: Optional[Decimal]
    budget_duration: Optional[str]
    budget_reset_at: Optional[datetime]
    last_evaluated_at: Optional[datetime]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @property
    def is_cost_budget(self) -> bool:
        return self.max_spend_usd is not None

    @property
    def is_iteration_budget(self) -> bool:
        return self.max_repeated_calls is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "name": self.name,
            "description": self.description,
            "scope": self.scope,
            "scope_value": self.scope_value,
            "max_spend_usd": str(self.max_spend_usd) if self.max_spend_usd is not None else None,
            "spent_usd": str(self.spent_usd),
            "soft_limit_ratio": (
                str(self.soft_limit_ratio) if self.soft_limit_ratio is not None else None
            ),
            "max_repeated_calls": self.max_repeated_calls,
            "loop_window_seconds": (
                str(self.loop_window_seconds) if self.loop_window_seconds is not None else None
            ),
            "budget_duration": self.budget_duration,
            "budget_reset_at": (
                self.budget_reset_at.isoformat() if self.budget_reset_at else None
            ),
            "last_evaluated_at": (
                self.last_evaluated_at.isoformat() if self.last_evaluated_at else None
            ),
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def _row_to_dto(row: Budget) -> BudgetRow:
    return BudgetRow(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        description=row.description,
        scope=row.scope,
        scope_value=row.scope_value,
        max_spend_usd=row.max_spend_usd,
        spent_usd=row.spent_usd or Decimal("0"),
        soft_limit_ratio=row.soft_limit_ratio,
        max_repeated_calls=row.max_repeated_calls,
        loop_window_seconds=row.loop_window_seconds,
        budget_duration=row.budget_duration,
        budget_reset_at=row.budget_reset_at,
        last_evaluated_at=row.last_evaluated_at,
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ---- Window arithmetic -------------------------------------------------


def compute_next_reset(now: datetime, duration: str) -> datetime:
    """Compute the next budget_reset_at boundary from now.

    Fixed-window reset, NOT rolling. A budget created mid-window has
    its first reset_at at now + duration; subsequent resets advance
    by exactly one duration each.

    This is deliberately simple — no calendar alignment (e.g. "reset
    at midnight UTC"), because operators creating a budget at 3:17pm
    typically don't want their first window to be 9 hours short.
    LiteLLM uses the same pattern. If operators want calendar
    alignment, they can create the budget at midnight.
    """
    if duration not in _DURATION_DELTA:
        raise ValueError(f"invalid duration {duration!r}")
    return now + _DURATION_DELTA[duration]


def window_start_from_reset(reset_at: datetime, duration: str) -> datetime:
    """Given a budget's reset_at and duration, return the corresponding
    window_start. Used by spend aggregation: spend covers
    [window_start, reset_at).
    """
    if duration not in _DURATION_DELTA:
        raise ValueError(f"invalid duration {duration!r}")
    return reset_at - _DURATION_DELTA[duration]


# ---- Validation -------------------------------------------------------


def _validate_create_args(
    *,
    scope: str,
    scope_value: Optional[str],
    max_spend_usd: Optional[Decimal],
    max_repeated_calls: Optional[int],
    loop_window_seconds: Optional[Decimal],
    budget_duration: Optional[str],
) -> None:
    """Raise ValueError on any invalid combination.

    Rules:
      * scope must be one of project|agent|model
      * scope=project requires scope_value is None
      * scope in (agent, model) requires non-empty scope_value
      * Exactly one of (max_spend_usd) or (max_repeated_calls +
        loop_window_seconds) must be set — cost or iteration, not both
      * Cost budgets require budget_duration
      * Iteration budgets require loop_window_seconds, not duration
    """
    if scope not in VALID_SCOPES:
        raise ValueError(
            f"invalid scope {scope!r}. Valid: {sorted(VALID_SCOPES)}"
        )
    if scope == "project":
        if scope_value:
            raise ValueError(
                "scope=project must not have a scope_value (applies to all)"
            )
    else:
        if not scope_value:
            raise ValueError(
                f"scope={scope} requires a non-empty scope_value"
            )

    # Direct None-checks rather than flag-based; mypy doesn't follow
    # the implication "is_cost True ⇒ max_spend_usd is not None" so the
    # original `if is_cost: max_spend_usd <= 0` was flagged. The flags
    # would only re-narrow with a redundant assert at the use site.
    if max_spend_usd is None and max_repeated_calls is None:
        raise ValueError(
            "budget must have either max_spend_usd (cost budget) or "
            "max_repeated_calls (iteration budget)"
        )
    if max_spend_usd is not None and max_repeated_calls is not None:
        raise ValueError(
            "budget cannot be both cost and iteration; create two separate budgets"
        )

    if max_spend_usd is not None:
        if max_spend_usd <= 0:
            raise ValueError("max_spend_usd must be positive")
        if not budget_duration:
            raise ValueError("cost budgets require budget_duration")
        if budget_duration not in VALID_DURATIONS:
            raise ValueError(
                f"invalid budget_duration {budget_duration!r}. "
                f"Valid: {sorted(VALID_DURATIONS)}"
            )

    if max_repeated_calls is not None:
        if max_repeated_calls <= 0:
            raise ValueError("max_repeated_calls must be positive")
        if loop_window_seconds is None or loop_window_seconds <= 0:
            raise ValueError(
                "iteration budgets require positive loop_window_seconds"
            )


# ---- create_budget ----------------------------------------------------


async def create_budget(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    name: str,
    scope: str,
    scope_value: Optional[str] = None,
    max_spend_usd: Optional[Decimal] = None,
    max_repeated_calls: Optional[int] = None,
    loop_window_seconds: Optional[Decimal] = None,
    budget_duration: Optional[str] = None,
    soft_limit_ratio: Optional[Decimal] = None,
    description: Optional[str] = None,
) -> BudgetRow:
    """Create a new budget. Validates inputs; computes budget_reset_at
    for cost budgets."""
    if not name or not name.strip():
        raise ValueError("name is required")

    _validate_create_args(
        scope=scope,
        scope_value=scope_value,
        max_spend_usd=max_spend_usd,
        max_repeated_calls=max_repeated_calls,
        loop_window_seconds=loop_window_seconds,
        budget_duration=budget_duration,
    )

    now = datetime.now(timezone.utc)
    budget_reset_at = None
    if max_spend_usd is not None:
        # _validate_create_args raised if a cost budget arrived without
        # a duration, so this is unreachable with None. The assert
        # documents the invariant and lets mypy narrow the type.
        assert budget_duration is not None, (
            "_validate_create_args should have rejected a cost budget "
            "without a duration"
        )
        budget_reset_at = compute_next_reset(now, budget_duration)

    values = {
        "project_id": project_id,
        "name": name.strip(),
        "description": description,
        "scope": scope,
        "scope_value": scope_value,
        "max_spend_usd": max_spend_usd,
        "max_repeated_calls": max_repeated_calls,
        "loop_window_seconds": loop_window_seconds,
        "budget_duration": budget_duration,
        "budget_reset_at": budget_reset_at,
        "soft_limit_ratio": soft_limit_ratio,
    }
    result = await session.execute(
        insert(Budget).values(**values).returning(Budget)
    )
    row = result.scalar_one()
    logger.info(
        "Created budget %s (project=%s scope=%s value=%s cost=%s iter=%s)",
        row.id, project_id, scope, scope_value or "(all)",
        max_spend_usd, max_repeated_calls,
    )
    return _row_to_dto(row)


# ---- list / get / delete ----------------------------------------------


async def list_budgets(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    include_inactive: bool = False,
    limit: int = 100,
) -> list[BudgetRow]:
    """List budgets for the project, newest first."""
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    stmt = select(Budget).where(Budget.project_id == project_id)
    if not include_inactive:
        stmt = stmt.where(Budget.is_active.is_(True))
    stmt = stmt.order_by(Budget.created_at.desc(), Budget.id.desc()).limit(limit)

    result = await session.scalars(stmt)
    return [_row_to_dto(r) for r in result.all()]


async def get_budget(
    session: AsyncSession,
    budget_id: uuid.UUID,
    project_id: uuid.UUID,
) -> Optional[BudgetRow]:
    """Fetch one budget. Returns None if not found OR not in project
    (cross-project lookups don't leak existence)."""
    row = await session.scalar(
        select(Budget).where(
            Budget.id == budget_id,
            Budget.project_id == project_id,
        )
    )
    return _row_to_dto(row) if row else None


async def delete_budget(
    session: AsyncSession,
    budget_id: uuid.UUID,
    project_id: uuid.UUID,
) -> bool:
    """Delete a budget. Returns True on success, False if not found.

    Hard delete (not soft). If we ever need audit history of deleted
    budgets, we add a budgets_audit table. For v1 the halt_state
    table preserves the audit trail of any halts the budget produced.
    """
    result = await session.execute(
        delete(Budget).where(
            Budget.id == budget_id,
            Budget.project_id == project_id,
        )
    )
    # rowcount exposed by the runtime CursorResult; SQLAlchemy 2.x
    # protocol stubs hide it.
    return result.rowcount > 0  # type: ignore[attr-defined]


# ---- Spend aggregation (THE budget evaluation query) ------------------


async def compute_spend_usd(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    scope: str,
    scope_value: Optional[str],
    window_start: datetime,
) -> Decimal:
    """Compute current spend in the window for a budget's scope.

    The hot path of the budget monitor. Runs every tick per active
    cost budget. Indexed scan on (project_id, end_time_unix_nano)
    WHERE cost_usd IS NOT NULL; for normal volumes (thousands of LLM
    spans in the window) completes in low single-digit milliseconds.

    Args:
        project_id      The budget's project
        scope           'project' | 'agent' | 'model'
        scope_value     agent_id / model_name / None
        window_start    Spans with end_time >= this are in scope

    Returns:
        Decimal sum of cost_usd over the matching spans, or Decimal(0)
        if no spans match. Never None.
    """
    window_start_ns = int(window_start.timestamp() * 1_000_000_000)

    conditions = [
        Span.project_id == project_id,
        Span.cost_usd.is_not(None),
        Span.end_time_unix_nano >= window_start_ns,
    ]

    if scope == "agent":
        if not scope_value:
            raise ValueError("scope=agent requires scope_value")
        conditions.append(Span.agent_id == scope_value)
    elif scope == "model":
        if not scope_value:
            raise ValueError("scope=model requires scope_value")
        conditions.append(Span.request_model == scope_value)
    elif scope != "project":
        raise ValueError(f"invalid scope {scope!r}")

    result = await session.scalar(
        select(func.coalesce(func.sum(Span.cost_usd), 0)).where(and_(*conditions))
    )
    return Decimal(result) if result is not None else Decimal("0")


async def compute_iteration_count(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    scope: str,
    scope_value: Optional[str],
    window_seconds: Decimal,
    now: Optional[datetime] = None,
) -> int:
    """Count tool spans in the rolling window for an iteration budget.

    Iteration budgets use a TRUE rolling window (not fixed-window-with-
    reset). Loop detection is about "in the last N seconds" — a
    runaway loop that survives a window-edge reset is still a runaway
    loop. The trade-off vs cost budgets is real: this query is harder
    to cache and the window keeps moving.

    Returns the count of tool_name-bearing spans matching the scope
    in the last ``window_seconds`` seconds.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    window_start = now - timedelta(seconds=float(window_seconds))
    window_start_ns = int(window_start.timestamp() * 1_000_000_000)

    conditions = [
        Span.project_id == project_id,
        Span.tool_name.is_not(None),
        Span.start_time_unix_nano >= window_start_ns,
    ]
    if scope == "agent":
        if not scope_value:
            raise ValueError("scope=agent requires scope_value")
        conditions.append(Span.agent_id == scope_value)
    elif scope == "model":
        if not scope_value:
            raise ValueError("scope=model requires scope_value")
        conditions.append(Span.request_model == scope_value)
    elif scope != "project":
        raise ValueError(f"invalid scope {scope!r}")

    result = await session.scalar(
        select(func.count()).select_from(Span).where(and_(*conditions))
    )
    return int(result or 0)


# ---- Monitor bookkeeping -----------------------------------------------


async def list_active_budgets_for_monitor(
    session: AsyncSession,
    limit: int = 1000,
) -> list[BudgetRow]:
    """Cross-project list for the monitor. Returns ALL active budgets.

    No project filter: the monitor evaluates every active budget
    across every project on each tick. For v1 single-replica deploys
    this is fine; at scale (thousands of budgets) we shard by
    project_id hash with multiple monitor workers.
    """
    if limit < 1:
        limit = 1
    if limit > 5000:
        limit = 5000

    stmt = (
        select(Budget)
        .where(Budget.is_active.is_(True))
        .order_by(Budget.last_evaluated_at.asc().nulls_first(), Budget.id.asc())
        .limit(limit)
    )
    result = await session.scalars(stmt)
    return [_row_to_dto(r) for r in result.all()]


async def update_monitor_state(
    session: AsyncSession,
    budget_id: uuid.UUID,
    *,
    spent_usd: Optional[Decimal] = None,
    budget_reset_at: Optional[datetime] = None,
    last_evaluated_at: Optional[datetime] = None,
) -> None:
    """Write the monitor's per-tick bookkeeping back to the row.

    Only the columns the caller passes are updated. The monitor
    typically updates all three on every tick (cached spent, advanced
    reset_at if crossed, current timestamp).
    """
    values: dict[str, Any] = {}
    if spent_usd is not None:
        values["spent_usd"] = spent_usd
    if budget_reset_at is not None:
        values["budget_reset_at"] = budget_reset_at
    if last_evaluated_at is not None:
        values["last_evaluated_at"] = last_evaluated_at
    if not values:
        return
    await session.execute(
        update(Budget).where(Budget.id == budget_id).values(**values)
    )


__all__ = [
    "BudgetRow",
    "VALID_DURATIONS",
    "VALID_SCOPES",
    "compute_iteration_count",
    "compute_next_reset",
    "compute_spend_usd",
    "create_budget",
    "delete_budget",
    "get_budget",
    "list_active_budgets_for_monitor",
    "list_budgets",
    "update_monitor_state",
    "window_start_from_reset",
]

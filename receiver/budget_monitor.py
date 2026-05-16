"""Budget monitor: periodic background task.

Ticks every ``STRATHON_BUDGET_EVAL_INTERVAL_SECONDS`` (default 5s),
evaluates each active budget, and produces or clears halts depending
on whether the budget is over threshold.

Architecture
============

Runs as an asyncio task in the receiver's lifespan, alongside the
retention sweeper and webhook sweeper. Same pattern, same shutdown
semantics, same operational story. No new dependencies — we
deliberately don't add Celery / ARQ / Redis just for a periodic tick.

Multi-replica safety
====================

When operators run multiple receiver replicas (the case the moment
they put one behind a load balancer), every replica would otherwise
run its own monitor tick. They'd all evaluate the same budgets at
the same time, each producing duplicate halts and racing to clear
each other's halts.

Postgres advisory locks solve this without an extra dependency.
Each tick wraps its work in ``pg_try_advisory_lock(MONITOR_LOCK_ID)``;
only one replica acquires the lock and runs the evaluation, the
others tick and immediately exit because the lock returned false.
The lock auto-releases when the holder disconnects, so a crashed
replica's lock disappears within seconds.

Caveat: PgBouncer in transaction-pooling mode is incompatible with
session-scoped advisory locks (per the research). The receiver
uses session pooling (or no pooler) by default; docs/self-hosting.md
documents this.

Halt lifecycle
==============

On each tick, for each active budget:

  1. If budget_reset_at is in the past (cost budget only): advance
     it by one duration AND clear any active halts this budget
     produced. Budget rolled over; the window's spend is now 0,
     so the halt no longer applies.

  2. Compute current spend (cost budget) or iteration count
     (iteration budget) over the active window.

  3. If over threshold:
       - If no active halt from this budget exists: create one.
       - If one exists: no-op (don't duplicate).

  4. If under threshold:
       - If an active halt from this budget exists AND it's a
         budget-monitor halt (actor=budget_monitor): clear it.
         (Operator halts are never auto-cleared even if their
         linked budget drops under threshold.)

  5. Stamp last_evaluated_at and cached spent_usd back on the row.

The link between a budget and the halt it produced is by
``halt_state.budget_id`` (the column exists from migration 001;
it's how a budget-produced halt identifies which budget triggered
it). Operator halts have budget_id=NULL.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import repositories.budgets as budgets_repo
from models.intervention import Budget, HaltState

logger = logging.getLogger("strathon.receiver.budget_monitor")


# Lock ID is an arbitrary stable int that identifies "the budget
# monitor's mutex." Different from any other advisory lock in the
# codebase (currently none, but reserved space starts at 1).
MONITOR_LOCK_ID = 0x5374726174686F30  # ASCII 'Strath0', truncated to int64
# Postgres advisory lock IDs are bigint (signed 64). The literal above
# fits. If we ever add more locks, allocate a small int-enum file.


@dataclass(frozen=True)
class MonitorConfig:
    """Tunables. All come from env vars with sane defaults."""
    tick_interval_seconds: float = 5.0
    # Per-tick cap on how many budgets a single replica evaluates.
    # Defensive: if a project has thousands of budgets, we don't
    # want one tick to take 30 seconds. Future commits add sharding.
    max_budgets_per_tick: int = 500

    @classmethod
    def from_env(cls) -> "MonitorConfig":
        return cls(
            tick_interval_seconds=float(
                os.environ.get("STRATHON_BUDGET_EVAL_INTERVAL_SECONDS", "5.0"),
            ),
            max_budgets_per_tick=int(
                os.environ.get("STRATHON_BUDGET_MAX_PER_TICK", "500"),
            ),
        )


# ---- The tick ---------------------------------------------------------


async def evaluate_one_budget(
    session: AsyncSession,
    budget: budgets_repo.BudgetRow,
    *,
    now: datetime,
) -> None:
    """Evaluate one budget and synchronize halt state.

    Caller is responsible for committing. We use one transaction per
    budget so a malformed budget doesn't take down the whole tick.
    """
    project_id = budget.project_id
    budget_id = budget.id

    # ---- Step 1: handle window rollover (cost budgets only) ----
    spent_usd: Optional[Decimal] = None
    iteration_count: Optional[int] = None
    over_threshold: bool

    new_reset_at: Optional[datetime] = budget.budget_reset_at

    if budget.is_cost_budget:
        if (
            budget.budget_reset_at is not None
            and budget.budget_reset_at <= now
            and budget.budget_duration
        ):
            # Window rolled over. Advance reset_at by one duration. If
            # we missed multiple windows (the monitor was down), keep
            # advancing until we land in the future.
            new_reset_at = budget.budget_reset_at
            while new_reset_at <= now:
                new_reset_at = budgets_repo.compute_next_reset(
                    new_reset_at, budget.budget_duration,
                )
            logger.info(
                "Budget %s window rolled over (new reset_at=%s)",
                budget_id, new_reset_at,
            )

        # Compute spend over the active window
        if new_reset_at is not None and budget.budget_duration:
            window_start = budgets_repo.window_start_from_reset(
                new_reset_at, budget.budget_duration,
            )
        else:
            # No window configured; treat as "since beginning of time"
            window_start = datetime(1970, 1, 1, tzinfo=timezone.utc)

        spent_usd = await budgets_repo.compute_spend_usd(
            session,
            project_id=project_id,
            scope=budget.scope,
            scope_value=budget.scope_value,
            window_start=window_start,
        )
        over_threshold = (
            budget.max_spend_usd is not None
            and spent_usd >= budget.max_spend_usd
        )

    elif budget.is_iteration_budget:
        iteration_count = await budgets_repo.compute_iteration_count(
            session,
            project_id=project_id,
            scope=budget.scope,
            scope_value=budget.scope_value,
            window_seconds=budget.loop_window_seconds,
            now=now,
        )
        over_threshold = iteration_count >= budget.max_repeated_calls

    else:
        # Neither type — shouldn't happen given create_budget's validation,
        # but defensive against operator direct-DB-edits.
        logger.warning(
            "Budget %s has neither max_spend_usd nor max_repeated_calls; "
            "skipping evaluation", budget_id,
        )
        return

    # ---- Step 2: look up existing halt produced by this budget ----
    existing_halt = await session.scalar(
        select(HaltState).where(
            HaltState.budget_id == budget_id,
            HaltState.actor == "budget_monitor",
            HaltState.state.in_(("paused", "halted")),
            HaltState.cleared_at.is_(None),
        )
    )

    # ---- Step 3: reconcile ----
    if over_threshold and existing_halt is None:
        # Create a new halt. We deliberately do NOT use halts_repo
        # (which is project/agent-scoped); the budget monitor halt
        # has budget_id set and the agent_id wildcard pattern that
        # the SDK's check_halt treats as a project halt.
        from repositories.halts import PROJECT_WILDCARD_AGENT_ID
        reason_kind = "cost" if budget.is_cost_budget else "iteration"
        if budget.is_cost_budget:
            reason = (
                f"Cost budget '{budget.name}' exceeded: "
                f"spent ${spent_usd} of ${budget.max_spend_usd}"
            )
        else:
            reason = (
                f"Iteration budget '{budget.name}' exceeded: "
                f"{iteration_count} calls in last {budget.loop_window_seconds}s, "
                f"max {budget.max_repeated_calls}"
            )
        await session.execute(
            insert(HaltState).values(
                project_id=project_id,
                budget_id=budget_id,
                # The halt_state CHECK requires one of
                # trace_id/agent_id/budget_id non-null; budget_id is
                # set, so the CHECK passes. The SDK's check_halt code
                # path doesn't natively understand budget-scope halts
                # in this commit (H4 will extend it). For v1 we still
                # need the SDK to see SOMETHING, so we ALSO populate
                # agent_id with the project-wildcard so the existing
                # project-scope code path picks it up.
                agent_id=PROJECT_WILDCARD_AGENT_ID,
                state="halted",
                reason=reason,
                actor="budget_monitor",
                halt_metadata={"budget_kind": reason_kind, "budget_id": str(budget_id)},
            )
        )
        logger.warning(
            "Budget %s over threshold; created halt (reason=%s)",
            budget_id, reason,
        )

    elif not over_threshold and existing_halt is not None:
        # Budget under threshold again; clear our halt.
        await session.execute(
            update(HaltState)
            .where(HaltState.id == existing_halt.id)
            .values(cleared_at=now)
        )
        logger.info(
            "Budget %s back under threshold; cleared halt %d",
            budget_id, existing_halt.id,
        )

    # ---- Step 4: bookkeeping ----
    update_kwargs: dict = {"last_evaluated_at": now}
    if budget.is_cost_budget:
        update_kwargs["spent_usd"] = spent_usd
        if new_reset_at != budget.budget_reset_at:
            update_kwargs["budget_reset_at"] = new_reset_at

    await session.execute(
        update(Budget).where(Budget.id == budget_id).values(**update_kwargs)
    )


# ---- Tick + loop -----------------------------------------------------


async def run_one_tick(
    session_maker: async_sessionmaker,
    config: MonitorConfig,
) -> int:
    """One full pass over active budgets.

    Returns the number of budgets evaluated (0 if we didn't hold the
    lock this tick). Each budget gets its own transaction so a single
    bad budget can't poison the whole tick.
    """
    # Acquire the advisory lock in its own short session so we hold
    # it only while evaluating (releases on session close).
    async with session_maker() as lock_session:
        lock_acquired = await lock_session.scalar(
            text("SELECT pg_try_advisory_lock(:k)").bindparams(k=MONITOR_LOCK_ID)
        )
        if not lock_acquired:
            # Another replica is running this tick
            return 0

        try:
            async with session_maker() as list_session:
                budgets = await budgets_repo.list_active_budgets_for_monitor(
                    list_session, limit=config.max_budgets_per_tick,
                )

            now = datetime.now(timezone.utc)
            count = 0
            for budget in budgets:
                try:
                    async with session_maker() as session:
                        await evaluate_one_budget(session, budget, now=now)
                        await session.commit()
                    count += 1
                except Exception:
                    logger.exception(
                        "Failed to evaluate budget %s; skipping", budget.id,
                    )
            return count
        finally:
            await lock_session.execute(
                text("SELECT pg_advisory_unlock(:k)").bindparams(k=MONITOR_LOCK_ID)
            )


async def monitor_loop(
    config: MonitorConfig,
    shutdown: asyncio.Event,
    session_maker: async_sessionmaker,
) -> None:
    """Top-level loop. Runs until shutdown is set."""
    logger.info(
        "Budget monitor starting (tick=%.2fs, max_per_tick=%d)",
        config.tick_interval_seconds, config.max_budgets_per_tick,
    )
    while not shutdown.is_set():
        try:
            evaluated = await run_one_tick(session_maker, config)
            if evaluated > 0:
                logger.debug("Budget monitor: evaluated %d budgets", evaluated)
        except Exception:
            # A bug in the monitor must NEVER take the receiver down.
            # Log and keep ticking; the next tick gets a clean shot.
            logger.exception("Budget monitor tick failed; continuing")

        try:
            await asyncio.wait_for(
                shutdown.wait(), timeout=config.tick_interval_seconds,
            )
        except asyncio.TimeoutError:
            continue
    logger.info("Budget monitor shutting down")


__all__ = [
    "MONITOR_LOCK_ID",
    "MonitorConfig",
    "evaluate_one_budget",
    "monitor_loop",
    "run_one_tick",
]

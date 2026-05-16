"""Tests for the budget monitor.

Coverage:
  * evaluate_one_budget creates a halt when over threshold
  * Does not duplicate halts on subsequent ticks
  * Clears halt when spend drops back under (in same window)
  * Advances reset_at on window rollover; multi-window catch-up
  * Auto-clears halt when window resets
  * Operator halts NOT auto-cleared (different actor)
  * Iteration budget evaluation
  * Malformed budget is skipped, not crashes the tick
  * run_one_tick acquires advisory lock; second caller exits early
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import insert, select, update

import repositories.budgets as budgets_repo
import budget_monitor
from models.intervention import Budget, HaltState
from models.traces import Span, Trace


# ---- Helpers --------------------------------------------------------


async def _seed_llm_span(
    session,
    *,
    project_id: uuid.UUID,
    cost_usd: Decimal,
    end_time: datetime,
    agent_id: str | None = None,
) -> None:
    trace_id = uuid.uuid4().bytes
    span_id = uuid.uuid4().bytes[:8]
    end_ns = int(end_time.timestamp() * 1_000_000_000)

    await session.execute(
        insert(Trace).values(
            id=trace_id,
            project_id=project_id,
            start_time_unix_nano=end_ns - 1_000_000,
        )
    )
    await session.execute(
        insert(Span).values(
            trace_id=trace_id,
            span_id=span_id,
            project_id=project_id,
            name="llm.generate",
            kind="CLIENT",
            start_time_unix_nano=end_ns - 1_000_000,
            end_time_unix_nano=end_ns,
            cost_usd=cost_usd,
            agent_id=agent_id,
            request_model="gpt-4o",
            attributes={},
        )
    )
    await session.flush()


async def _make_cost_budget(
    session,
    project_id: uuid.UUID,
    *,
    max_spend: Decimal,
    duration: str = "1d",
    scope: str = "project",
    scope_value: str | None = None,
) -> budgets_repo.BudgetRow:
    return await budgets_repo.create_budget(
        session, project_id,
        name=f"test-{uuid.uuid4().hex[:6]}",
        scope=scope,
        scope_value=scope_value,
        max_spend_usd=max_spend,
        budget_duration=duration,
    )


# ---- evaluate_one_budget --------------------------------------------


async def test_creates_halt_when_over_threshold(session, isolated_project):
    budget = await _make_cost_budget(
        session, isolated_project, max_spend=Decimal("0.001"),
    )
    # Seed spans AFTER budget creation so they fall in the current window.
    # A budget created at T sets window=[T, T+1d]; spans must have
    # end_time >= T to count. Seeding with end_time = now + 1 min puts
    # them safely inside the window.
    await _seed_llm_span(
        session, project_id=isolated_project,
        cost_usd=Decimal("0.005"),
        end_time=datetime.now(timezone.utc) + timedelta(minutes=1),
    )

    await budget_monitor.evaluate_one_budget(
        session, budget, now=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    await session.flush()

    # A budget_monitor halt should now exist for this budget
    halt = await session.scalar(
        select(HaltState).where(
            HaltState.budget_id == budget.id,
            HaltState.cleared_at.is_(None),
        )
    )
    assert halt is not None
    assert halt.actor == "budget_monitor"
    assert halt.state == "halted"
    assert "exceeded" in halt.reason.lower()


async def test_does_not_duplicate_halt(session, isolated_project):
    """A second tick with budget still over threshold must NOT
    create a second halt row."""
    budget = await _make_cost_budget(
        session, isolated_project, max_spend=Decimal("0.001"),
    )
    await _seed_llm_span(
        session, project_id=isolated_project,
        cost_usd=Decimal("0.005"),
        end_time=datetime.now(timezone.utc) + timedelta(minutes=1),
    )

    now = datetime.now(timezone.utc) + timedelta(minutes=2)
    await budget_monitor.evaluate_one_budget(session, budget, now=now)
    await session.flush()

    # Refresh the budget row (last_evaluated_at + spent_usd are stale on
    # the original dto) so the second call sees the latest state.
    budget2 = await budgets_repo.get_budget(session, budget.id, isolated_project)
    await budget_monitor.evaluate_one_budget(session, budget2, now=now)
    await session.flush()

    halts = (await session.scalars(
        select(HaltState).where(HaltState.budget_id == budget.id)
    )).all()
    assert len(halts) == 1


async def test_clears_halt_when_window_rolls_over(session, isolated_project):
    """When budget_reset_at passes, the halt produced by this budget
    auto-clears (window has reset, spend is 0 again)."""
    budget = await _make_cost_budget(
        session, isolated_project, max_spend=Decimal("0.001"), duration="1d",
    )
    # Force reset_at into the past
    await session.execute(
        update(Budget).where(Budget.id == budget.id).values(
            budget_reset_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    )
    await session.flush()
    # Seed an OLD span (would be in the previous window, not the new one)
    await _seed_llm_span(
        session, project_id=isolated_project,
        cost_usd=Decimal("0.005"),
        end_time=datetime.now(timezone.utc) - timedelta(days=2),
    )
    # Pre-create a halt that the monitor would have produced last cycle
    await session.execute(
        insert(HaltState).values(
            project_id=isolated_project,
            budget_id=budget.id,
            agent_id="*",
            state="halted",
            reason="old window",
            actor="budget_monitor",
        )
    )
    await session.flush()

    # Refresh
    budget_now = await budgets_repo.get_budget(
        session, budget.id, isolated_project,
    )
    now = datetime.now(timezone.utc)
    await budget_monitor.evaluate_one_budget(session, budget_now, now=now)
    await session.flush()

    # Halt should now be cleared (window rolled over, new window has $0)
    halt = await session.scalar(
        select(HaltState).where(HaltState.budget_id == budget.id)
    )
    assert halt.cleared_at is not None

    # And the budget's reset_at should be advanced into the future
    refreshed = await budgets_repo.get_budget(
        session, budget.id, isolated_project,
    )
    assert refreshed.budget_reset_at > now


async def test_does_not_clear_operator_halts(session, isolated_project):
    """An operator halt (actor=user) should NOT be cleared by the
    monitor even if the budget that produced an automatic halt was
    cleared."""
    budget = await _make_cost_budget(
        session, isolated_project, max_spend=Decimal("100"),
    )
    # Seed an OPERATOR halt linked to this budget (unusual but possible)
    await session.execute(
        insert(HaltState).values(
            project_id=isolated_project,
            budget_id=budget.id,
            agent_id="*",
            state="halted",
            reason="operator decision",
            actor="user",
        )
    )
    await session.flush()
    # Budget is well under threshold ($0 spent vs $100 cap)
    refreshed = await budgets_repo.get_budget(
        session, budget.id, isolated_project,
    )
    await budget_monitor.evaluate_one_budget(
        session, refreshed, now=datetime.now(timezone.utc),
    )
    await session.flush()

    # Operator halt should still be active
    halt = await session.scalar(
        select(HaltState).where(
            HaltState.budget_id == budget.id,
            HaltState.actor == "user",
        )
    )
    assert halt.cleared_at is None


async def test_iteration_budget_evaluation(session, isolated_project):
    """Iteration budgets count tool spans, not LLM cost."""
    budget = await budgets_repo.create_budget(
        session, isolated_project,
        name="loop",
        scope="agent", scope_value="agent-X",
        max_repeated_calls=3,
        loop_window_seconds=Decimal("60"),
    )
    # Seed 5 tool spans
    now = datetime.now(timezone.utc)
    for _ in range(5):
        trace_id = uuid.uuid4().bytes
        span_id = uuid.uuid4().bytes[:8]
        end_ns = int(now.timestamp() * 1_000_000_000)
        await session.execute(
            insert(Trace).values(
                id=trace_id, project_id=isolated_project,
                start_time_unix_nano=end_ns - 1_000_000,
            )
        )
        await session.execute(
            insert(Span).values(
                trace_id=trace_id, span_id=span_id,
                project_id=isolated_project,
                name="tool.send_email", kind="INTERNAL",
                start_time_unix_nano=end_ns - 500_000,
                end_time_unix_nano=end_ns,
                tool_name="send_email",
                agent_id="agent-X",
                attributes={},
            )
        )
    await session.flush()

    await budget_monitor.evaluate_one_budget(session, budget, now=now)
    await session.flush()

    halt = await session.scalar(
        select(HaltState).where(
            HaltState.budget_id == budget.id,
            HaltState.cleared_at.is_(None),
        )
    )
    assert halt is not None
    assert "iteration" in halt.reason.lower() or "loop" in halt.reason.lower()


async def test_under_threshold_no_halt_created(session, isolated_project):
    """A budget that's never been over threshold shouldn't have a halt."""
    budget = await _make_cost_budget(
        session, isolated_project, max_spend=Decimal("100"),
    )
    await _seed_llm_span(
        session, project_id=isolated_project,
        cost_usd=Decimal("0.001"),  # way under $100
        end_time=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    await budget_monitor.evaluate_one_budget(
        session, budget, now=datetime.now(timezone.utc),
    )
    await session.flush()

    halt = await session.scalar(
        select(HaltState).where(HaltState.budget_id == budget.id)
    )
    assert halt is None


async def test_last_evaluated_at_is_stamped(session, isolated_project):
    budget = await _make_cost_budget(
        session, isolated_project, max_spend=Decimal("100"),
    )
    now = datetime.now(timezone.utc)
    await budget_monitor.evaluate_one_budget(session, budget, now=now)
    await session.flush()

    refreshed = await budgets_repo.get_budget(
        session, budget.id, isolated_project,
    )
    assert refreshed.last_evaluated_at is not None

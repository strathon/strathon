"""Tests for the budgets repository.

Coverage:
  * create_budget validates inputs (scope, scope_value, exclusive cost
    vs iteration, positive amounts, valid duration)
  * Listing returns project's budgets only, newest first
  * get_budget enforces cross-project isolation
  * delete_budget returns bool
  * compute_spend_usd aggregates correctly across:
      - project scope
      - agent scope
      - model scope
    and respects window_start
  * compute_iteration_count is a true rolling window
  * compute_next_reset / window_start_from_reset arithmetic
  * list_active_budgets_for_monitor returns all active across projects
  * update_monitor_state writes the bookkeeping
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import insert

import repositories.budgets as budgets_repo
from models.intervention import Budget
from models.traces import Span, Trace


# ---- Fixtures ---------------------------------------------------------


async def _seed_span(
    session,
    *,
    project_id: uuid.UUID,
    trace_id: bytes,
    span_id: bytes,
    cost_usd: Decimal | None,
    end_time: datetime,
    agent_id: str | None = None,
    request_model: str | None = None,
    tool_name: str | None = None,
) -> None:
    # Make sure the parent trace row exists.
    existing_trace = await session.scalar(
        Trace.__table__.select().where(Trace.id == trace_id)
    )
    if existing_trace is None:
        await session.execute(
            insert(Trace).values(
                id=trace_id,
                project_id=project_id,
                start_time_unix_nano=int(end_time.timestamp() * 1_000_000_000) - 1_000_000,
            )
        )

    end_ns = int(end_time.timestamp() * 1_000_000_000)
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
            request_model=request_model,
            tool_name=tool_name,
            attributes={},
        )
    )
    await session.flush()


@pytest_asyncio.fixture
async def second_project(session):
    """A second project for cross-project isolation tests."""
    from models import Project, ProjectSettings
    pid = uuid.uuid4()
    await session.execute(
        insert(Project).values(
            org_id=__import__("uuid").UUID("00000000-0000-0000-0000-0000000000aa"), id=pid, name=f"Other {pid.hex[:6]}", slug=f"other-{pid.hex[:8]}",
        )
    )
    await session.execute(insert(ProjectSettings).values(project_id=pid))
    await session.flush()
    return pid


# ---- create_budget validation -----------------------------------------


async def test_create_cost_budget_basic(session, isolated_project):
    b = await budgets_repo.create_budget(
        session, isolated_project,
        name="monthly cost",
        scope="project",
        max_spend_usd=Decimal("100"),
        budget_duration="30d",
    )
    assert b.name == "monthly cost"
    assert b.scope == "project"
    assert b.scope_value is None
    assert b.max_spend_usd == Decimal("100")
    assert b.budget_duration == "30d"
    assert b.budget_reset_at is not None
    assert b.is_active is True
    assert b.is_cost_budget
    assert not b.is_iteration_budget


async def test_create_iteration_budget(session, isolated_project):
    b = await budgets_repo.create_budget(
        session, isolated_project,
        name="loop guard",
        scope="agent",
        scope_value="agent-7",
        max_repeated_calls=50,
        loop_window_seconds=Decimal("60"),
    )
    assert b.is_iteration_budget
    assert b.max_repeated_calls == 50
    assert b.scope_value == "agent-7"
    # Iteration budgets don't have a reset_at (rolling window)
    assert b.budget_reset_at is None


async def test_create_rejects_both_cost_and_iteration(session, isolated_project):
    with pytest.raises(ValueError, match="cannot be both"):
        await budgets_repo.create_budget(
            session, isolated_project,
            name="bad",
            scope="project",
            max_spend_usd=Decimal("10"),
            budget_duration="1d",
            max_repeated_calls=5,
            loop_window_seconds=Decimal("30"),
        )


async def test_create_rejects_neither(session, isolated_project):
    with pytest.raises(ValueError, match="must have either"):
        await budgets_repo.create_budget(
            session, isolated_project,
            name="bad",
            scope="project",
        )


async def test_create_rejects_project_scope_with_value(session, isolated_project):
    with pytest.raises(ValueError, match="must not have a scope_value"):
        await budgets_repo.create_budget(
            session, isolated_project,
            name="bad",
            scope="project",
            scope_value="something",
            max_spend_usd=Decimal("10"),
            budget_duration="1d",
        )


async def test_create_rejects_agent_scope_without_value(session, isolated_project):
    with pytest.raises(ValueError, match="requires a non-empty scope_value"):
        await budgets_repo.create_budget(
            session, isolated_project,
            name="bad",
            scope="agent",
            max_spend_usd=Decimal("10"),
            budget_duration="1d",
        )


async def test_create_rejects_invalid_scope(session, isolated_project):
    with pytest.raises(ValueError, match="invalid scope"):
        await budgets_repo.create_budget(
            session, isolated_project,
            name="bad",
            scope="user",  # not in v1
            scope_value="alice",
            max_spend_usd=Decimal("10"),
            budget_duration="1d",
        )


async def test_create_rejects_negative_spend(session, isolated_project):
    with pytest.raises(ValueError, match="must be positive"):
        await budgets_repo.create_budget(
            session, isolated_project,
            name="bad",
            scope="project",
            max_spend_usd=Decimal("-1"),
            budget_duration="1d",
        )


async def test_create_rejects_invalid_duration(session, isolated_project):
    with pytest.raises(ValueError, match="invalid budget_duration"):
        await budgets_repo.create_budget(
            session, isolated_project,
            name="bad",
            scope="project",
            max_spend_usd=Decimal("10"),
            budget_duration="2w",  # not in v1
        )


async def test_create_cost_budget_requires_duration(session, isolated_project):
    with pytest.raises(ValueError, match="cost budgets require budget_duration"):
        await budgets_repo.create_budget(
            session, isolated_project,
            name="bad",
            scope="project",
            max_spend_usd=Decimal("10"),
        )


async def test_create_iteration_budget_requires_window(session, isolated_project):
    with pytest.raises(ValueError, match="positive loop_window_seconds"):
        await budgets_repo.create_budget(
            session, isolated_project,
            name="bad",
            scope="project",
            max_repeated_calls=5,
        )


# ---- list / get / delete ---------------------------------------------


async def test_list_returns_project_budgets_only(
    session, isolated_project, second_project,
):
    await budgets_repo.create_budget(
        session, isolated_project,
        name="mine", scope="project",
        max_spend_usd=Decimal("100"), budget_duration="1d",
    )
    await budgets_repo.create_budget(
        session, second_project,
        name="theirs", scope="project",
        max_spend_usd=Decimal("100"), budget_duration="1d",
    )
    mine = await budgets_repo.list_budgets(session, isolated_project)
    theirs = await budgets_repo.list_budgets(session, second_project)
    assert len(mine) == 1
    assert mine[0].name == "mine"
    assert len(theirs) == 1
    assert theirs[0].name == "theirs"


async def test_get_returns_none_for_cross_project(
    session, isolated_project, second_project,
):
    b = await budgets_repo.create_budget(
        session, isolated_project,
        name="mine", scope="project",
        max_spend_usd=Decimal("100"), budget_duration="1d",
    )
    # Cross-project lookup returns None — doesn't leak existence
    result = await budgets_repo.get_budget(session, b.id, second_project)
    assert result is None


async def test_delete_returns_false_for_nonexistent(session, isolated_project):
    deleted = await budgets_repo.delete_budget(session, uuid.uuid4(), isolated_project)
    assert deleted is False


async def test_delete_returns_true_for_existing(session, isolated_project):
    b = await budgets_repo.create_budget(
        session, isolated_project,
        name="x", scope="project",
        max_spend_usd=Decimal("100"), budget_duration="1d",
    )
    deleted = await budgets_repo.delete_budget(session, b.id, isolated_project)
    assert deleted is True
    # And it's actually gone
    assert await budgets_repo.get_budget(session, b.id, isolated_project) is None


# ---- Window math -----------------------------------------------------


def test_compute_next_reset_1d():
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)
    nxt = budgets_repo.compute_next_reset(now, "1d")
    assert nxt == datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)


def test_compute_next_reset_30d():
    now = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    nxt = budgets_repo.compute_next_reset(now, "30d")
    assert nxt == datetime(2026, 5, 31, 0, 0, 0, tzinfo=timezone.utc)


def test_window_start_round_trip():
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)
    nxt = budgets_repo.compute_next_reset(now, "7d")
    start = budgets_repo.window_start_from_reset(nxt, "7d")
    assert start == now


def test_compute_next_reset_rejects_bad_duration():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        budgets_repo.compute_next_reset(now, "2w")


# ---- Spend aggregation -----------------------------------------------


async def test_spend_aggregation_project_scope(session, isolated_project):
    """Project-scope budget sums every LLM span in the window."""
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=1)

    # Two LLM spans, total $0.0075
    await _seed_span(
        session,
        project_id=isolated_project,
        trace_id=uuid.uuid4().bytes,
        span_id=b"\x01" * 8,
        cost_usd=Decimal("0.005"),
        end_time=now - timedelta(minutes=30),
        request_model="gpt-4o",
        agent_id="a1",
    )
    await _seed_span(
        session,
        project_id=isolated_project,
        trace_id=uuid.uuid4().bytes,
        span_id=b"\x02" * 8,
        cost_usd=Decimal("0.0025"),
        end_time=now - timedelta(minutes=15),
        request_model="claude-3-5-sonnet",
        agent_id="a2",
    )

    spent = await budgets_repo.compute_spend_usd(
        session,
        project_id=isolated_project,
        scope="project",
        scope_value=None,
        window_start=window_start,
    )
    assert spent == Decimal("0.0075")


async def test_spend_aggregation_agent_scope(session, isolated_project):
    """Agent-scope budget only counts spans where agent_id matches."""
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=1)

    await _seed_span(
        session, project_id=isolated_project,
        trace_id=uuid.uuid4().bytes, span_id=b"\x01" * 8,
        cost_usd=Decimal("0.005"), end_time=now - timedelta(minutes=30),
        agent_id="agent-A",
    )
    await _seed_span(
        session, project_id=isolated_project,
        trace_id=uuid.uuid4().bytes, span_id=b"\x02" * 8,
        cost_usd=Decimal("0.003"), end_time=now - timedelta(minutes=20),
        agent_id="agent-B",
    )

    spent_a = await budgets_repo.compute_spend_usd(
        session, project_id=isolated_project,
        scope="agent", scope_value="agent-A", window_start=window_start,
    )
    assert spent_a == Decimal("0.005")

    spent_b = await budgets_repo.compute_spend_usd(
        session, project_id=isolated_project,
        scope="agent", scope_value="agent-B", window_start=window_start,
    )
    assert spent_b == Decimal("0.003")


async def test_spend_aggregation_model_scope(session, isolated_project):
    """Model-scope budget filters by request_model."""
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=1)

    await _seed_span(
        session, project_id=isolated_project,
        trace_id=uuid.uuid4().bytes, span_id=b"\x01" * 8,
        cost_usd=Decimal("0.01"), end_time=now - timedelta(minutes=30),
        request_model="gpt-4o",
    )
    await _seed_span(
        session, project_id=isolated_project,
        trace_id=uuid.uuid4().bytes, span_id=b"\x02" * 8,
        cost_usd=Decimal("0.005"), end_time=now - timedelta(minutes=15),
        request_model="gpt-4o-mini",
    )

    spent_4o = await budgets_repo.compute_spend_usd(
        session, project_id=isolated_project,
        scope="model", scope_value="gpt-4o", window_start=window_start,
    )
    assert spent_4o == Decimal("0.01")


async def test_spend_aggregation_excludes_outside_window(session, isolated_project):
    """Spans outside the window must not be counted."""
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=1)

    await _seed_span(
        session, project_id=isolated_project,
        trace_id=uuid.uuid4().bytes, span_id=b"\x01" * 8,
        cost_usd=Decimal("0.01"),
        end_time=now - timedelta(hours=2),  # OUTSIDE window
    )
    await _seed_span(
        session, project_id=isolated_project,
        trace_id=uuid.uuid4().bytes, span_id=b"\x02" * 8,
        cost_usd=Decimal("0.005"),
        end_time=now - timedelta(minutes=15),  # inside
    )
    spent = await budgets_repo.compute_spend_usd(
        session, project_id=isolated_project,
        scope="project", scope_value=None, window_start=window_start,
    )
    assert spent == Decimal("0.005")


async def test_spend_aggregation_returns_zero_when_no_spans(
    session, isolated_project,
):
    spent = await budgets_repo.compute_spend_usd(
        session, project_id=isolated_project,
        scope="project", scope_value=None,
        window_start=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert spent == Decimal("0")


# ---- Iteration count -------------------------------------------------


async def test_iteration_count_rolling_window(session, isolated_project):
    """Counts tool spans in the last N seconds. Rolling, not fixed."""
    now = datetime.now(timezone.utc)

    # 3 tool spans inside the window
    for i in range(3):
        await _seed_span(
            session, project_id=isolated_project,
            trace_id=uuid.uuid4().bytes, span_id=bytes([i + 10]) * 8,
            cost_usd=None,  # tool spans don't have cost
            end_time=now - timedelta(seconds=10),
            tool_name="send_email",
            agent_id="agent-X",
        )
    # 1 outside the window
    await _seed_span(
        session, project_id=isolated_project,
        trace_id=uuid.uuid4().bytes, span_id=b"\xff" * 8,
        cost_usd=None,
        end_time=now - timedelta(seconds=120),
        tool_name="send_email",
        agent_id="agent-X",
    )

    count = await budgets_repo.compute_iteration_count(
        session, project_id=isolated_project,
        scope="agent", scope_value="agent-X",
        window_seconds=Decimal("60"),
        now=now,
    )
    assert count == 3


async def test_iteration_count_ignores_non_tool_spans(session, isolated_project):
    """LLM spans (no tool_name) don't count toward iteration."""
    now = datetime.now(timezone.utc)
    await _seed_span(
        session, project_id=isolated_project,
        trace_id=uuid.uuid4().bytes, span_id=b"\x01" * 8,
        cost_usd=Decimal("0.001"),
        end_time=now - timedelta(seconds=10),
        request_model="gpt-4o",
        agent_id="agent-X",
        # tool_name is None
    )
    count = await budgets_repo.compute_iteration_count(
        session, project_id=isolated_project,
        scope="agent", scope_value="agent-X",
        window_seconds=Decimal("60"),
        now=now,
    )
    assert count == 0


# ---- Monitor bookkeeping ---------------------------------------------


async def test_list_active_budgets_for_monitor_skips_inactive(
    session, isolated_project,
):
    b1 = await budgets_repo.create_budget(
        session, isolated_project,
        name="active", scope="project",
        max_spend_usd=Decimal("100"), budget_duration="1d",
    )
    b2 = await budgets_repo.create_budget(
        session, isolated_project,
        name="inactive", scope="project",
        max_spend_usd=Decimal("100"), budget_duration="1d",
    )
    # Deactivate b2
    from sqlalchemy import update
    await session.execute(
        update(Budget).where(Budget.id == b2.id).values(is_active=False)
    )
    await session.flush()

    rows = await budgets_repo.list_active_budgets_for_monitor(session)
    ids = {r.id for r in rows}
    assert b1.id in ids
    assert b2.id not in ids


async def test_update_monitor_state_writes_partial(session, isolated_project):
    b = await budgets_repo.create_budget(
        session, isolated_project,
        name="x", scope="project",
        max_spend_usd=Decimal("100"), budget_duration="1d",
    )
    now = datetime.now(timezone.utc)
    await budgets_repo.update_monitor_state(
        session, b.id,
        spent_usd=Decimal("42.50"),
        last_evaluated_at=now,
    )
    await session.flush()

    refreshed = await budgets_repo.get_budget(session, b.id, isolated_project)
    assert refreshed.spent_usd == Decimal("42.50")
    assert refreshed.last_evaluated_at is not None

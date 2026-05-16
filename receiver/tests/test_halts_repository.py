"""Repository tests for halts.

Coverage:
  - create_halt: agent + project scope, validation errors, state options
  - list_active_halts: filtering active vs cleared, ordering
  - get_halt: not-found and cross-project isolation
  - clear_halt: sets cleared_at, refuses re-clear, returns None for missing
  - get_active_halts_for_sync: compact SDK payload, skips non-operator scopes
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)

from sqlalchemy import insert, delete  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from models import Project, ProjectSettings  # noqa: E402
from models.intervention import HaltState  # noqa: E402
from repositories import halts as halts_repo  # noqa: E402


# ---- create_halt -------------------------------------------------------


async def test_create_halt_agent_scope(session, isolated_project):
    halt = await halts_repo.create_halt(
        session, isolated_project,
        scope="agent", scope_value="agent-7",
        reason="test halt",
    )
    assert halt.scope == "agent"
    assert halt.scope_value == "agent-7"
    assert halt.state == "halted"
    assert halt.actor == "user"
    assert halt.cleared_at is None


async def test_create_halt_project_scope(session, isolated_project):
    halt = await halts_repo.create_halt(
        session, isolated_project,
        scope="project", scope_value=None,
        reason="full project halt",
    )
    assert halt.scope == "project"
    assert halt.scope_value is None  # operator-facing form
    # The underlying row carries the wildcard, but operators don't see it


async def test_create_halt_rejects_unknown_scope(session, isolated_project):
    with pytest.raises(ValueError, match="invalid scope"):
        await halts_repo.create_halt(
            session, isolated_project,
            scope="trace", scope_value="abc",
            reason="x",
        )


async def test_create_halt_rejects_agent_without_value(session, isolated_project):
    with pytest.raises(ValueError, match="requires a non-empty scope_value"):
        await halts_repo.create_halt(
            session, isolated_project,
            scope="agent", scope_value=None,
            reason="x",
        )


async def test_create_halt_rejects_project_with_value(session, isolated_project):
    with pytest.raises(ValueError, match="must not have a scope_value"):
        await halts_repo.create_halt(
            session, isolated_project,
            scope="project", scope_value="something",
            reason="x",
        )


async def test_create_halt_rejects_empty_reason(session, isolated_project):
    with pytest.raises(ValueError, match="reason is required"):
        await halts_repo.create_halt(
            session, isolated_project,
            scope="agent", scope_value="a",
            reason="   ",  # whitespace-only is also empty
        )


async def test_create_halt_accepts_paused_state(session, isolated_project):
    halt = await halts_repo.create_halt(
        session, isolated_project,
        scope="agent", scope_value="agent-x",
        reason="pause for inspection", state="paused",
    )
    assert halt.state == "paused"


async def test_create_halt_rejects_resumed_state(session, isolated_project):
    """resumed/cleared aren't valid initial states — those are
    state transitions, not initial halts."""
    with pytest.raises(ValueError, match="invalid state"):
        await halts_repo.create_halt(
            session, isolated_project,
            scope="agent", scope_value="x",
            reason="x", state="resumed",
        )


# ---- list_active_halts -------------------------------------------------


async def test_list_returns_empty_for_empty_project(session, isolated_project):
    halts = await halts_repo.list_active_halts(session, isolated_project)
    assert halts == []


async def test_list_returns_active_halts_newest_first(session, isolated_project):
    await halts_repo.create_halt(
        session, isolated_project,
        scope="agent", scope_value="a", reason="first",
    )
    await halts_repo.create_halt(
        session, isolated_project,
        scope="agent", scope_value="b", reason="second",
    )
    halts = await halts_repo.list_active_halts(session, isolated_project)
    assert len(halts) == 2
    # Newest first
    assert halts[0].reason == "second"
    assert halts[1].reason == "first"


async def test_list_excludes_cleared_by_default(session, isolated_project):
    """An operator listing halts wants active ones; the cleared ones
    are audit history."""
    h1 = await halts_repo.create_halt(
        session, isolated_project,
        scope="agent", scope_value="a", reason="r1",
    )
    h2 = await halts_repo.create_halt(
        session, isolated_project,
        scope="agent", scope_value="b", reason="r2",
    )
    await halts_repo.clear_halt(session, h1.id, isolated_project)

    halts = await halts_repo.list_active_halts(session, isolated_project)
    ids = [h.id for h in halts]
    assert h2.id in ids
    assert h1.id not in ids


async def test_list_include_cleared_returns_audit_trail(session, isolated_project):
    h1 = await halts_repo.create_halt(
        session, isolated_project,
        scope="agent", scope_value="a", reason="r1",
    )
    await halts_repo.clear_halt(session, h1.id, isolated_project)
    halts = await halts_repo.list_active_halts(
        session, isolated_project, include_cleared=True,
    )
    ids = [h.id for h in halts]
    assert h1.id in ids


async def test_list_scopes_to_project(session, isolated_project, async_engine):
    """A halt in project A must not appear in a list for project B."""
    other_pid = uuid.uuid4()
    async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
        await s.execute(insert(Project).values(
            id=other_pid, name="other-halt", slug=f"other-halt-{other_pid.hex[:8]}",
        ))
        await s.execute(insert(ProjectSettings).values(project_id=other_pid))
        await halts_repo.create_halt(
            s, other_pid,
            scope="agent", scope_value="x", reason="other project",
        )
        await s.commit()

    try:
        halts = await halts_repo.list_active_halts(session, isolated_project)
        # Test sees zero from the other project
        other_reasons = [h for h in halts if h.reason == "other project"]
        assert other_reasons == []
    finally:
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            await s.execute(delete(HaltState).where(HaltState.project_id == other_pid))
            await s.execute(
                delete(ProjectSettings).where(ProjectSettings.project_id == other_pid),
            )
            await s.execute(delete(Project).where(Project.id == other_pid))
            await s.commit()


# ---- get_halt ----------------------------------------------------------


async def test_get_halt_returns_dto(session, isolated_project):
    created = await halts_repo.create_halt(
        session, isolated_project,
        scope="agent", scope_value="a", reason="r",
    )
    got = await halts_repo.get_halt(session, created.id, isolated_project)
    assert got is not None
    assert got.id == created.id
    assert got.reason == "r"


async def test_get_halt_unknown_returns_none(session, isolated_project):
    got = await halts_repo.get_halt(session, 99999, isolated_project)
    assert got is None


async def test_get_halt_wrong_project_returns_none(
    session, isolated_project, async_engine,
):
    other_pid = uuid.uuid4()
    halt_id = None
    async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
        await s.execute(insert(Project).values(
            id=other_pid, name="other-get", slug=f"other-get-{other_pid.hex[:8]}",
        ))
        await s.execute(insert(ProjectSettings).values(project_id=other_pid))
        h = await halts_repo.create_halt(
            s, other_pid,
            scope="agent", scope_value="x", reason="other",
        )
        halt_id = h.id
        await s.commit()
    try:
        got = await halts_repo.get_halt(session, halt_id, isolated_project)
        assert got is None  # cross-project leak prevented
    finally:
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            await s.execute(delete(HaltState).where(HaltState.project_id == other_pid))
            await s.execute(
                delete(ProjectSettings).where(ProjectSettings.project_id == other_pid),
            )
            await s.execute(delete(Project).where(Project.id == other_pid))
            await s.commit()


# ---- clear_halt --------------------------------------------------------


async def test_clear_halt_sets_cleared_at(session, isolated_project):
    h = await halts_repo.create_halt(
        session, isolated_project,
        scope="agent", scope_value="a", reason="r",
    )
    cleared = await halts_repo.clear_halt(session, h.id, isolated_project)
    assert cleared is not None
    assert cleared.cleared_at is not None


async def test_clear_halt_unknown_returns_none(session, isolated_project):
    got = await halts_repo.clear_halt(session, 99999, isolated_project)
    assert got is None


async def test_clear_halt_already_cleared_raises(session, isolated_project):
    h = await halts_repo.create_halt(
        session, isolated_project,
        scope="agent", scope_value="a", reason="r",
    )
    await halts_repo.clear_halt(session, h.id, isolated_project)
    with pytest.raises(ValueError, match="already cleared"):
        await halts_repo.clear_halt(session, h.id, isolated_project)


# ---- get_active_halts_for_sync (SDK payload shape) -------------------


async def test_sync_returns_compact_payload(session, isolated_project):
    await halts_repo.create_halt(
        session, isolated_project,
        scope="agent", scope_value="agent-7", reason="killswitch",
    )
    payload = await halts_repo.get_active_halts_for_sync(
        session, isolated_project,
    )
    assert len(payload) == 1
    entry = payload[0]
    # Exactly the fields the SDK needs
    assert set(entry.keys()) == {"id", "scope", "scope_value", "state", "reason"}
    assert entry["scope"] == "agent"
    assert entry["scope_value"] == "agent-7"
    assert entry["state"] == "halted"


async def test_sync_excludes_cleared_halts(session, isolated_project):
    h = await halts_repo.create_halt(
        session, isolated_project,
        scope="agent", scope_value="a", reason="r",
    )
    await halts_repo.clear_halt(session, h.id, isolated_project)
    payload = await halts_repo.get_active_halts_for_sync(
        session, isolated_project,
    )
    # Test's halt was cleared; should not appear. But other tests in
    # the same run may have added rows to the default project that
    # haven't been cleared, so we filter to our own.
    assert h.id not in [p["id"] for p in payload]


async def test_sync_project_scope_value_is_none(session, isolated_project):
    """Project-scope halts must surface as scope_value=None in the SDK
    payload, not the internal '*' sentinel."""
    await halts_repo.create_halt(
        session, isolated_project,
        scope="project", scope_value=None,
        reason="full halt",
    )
    payload = await halts_repo.get_active_halts_for_sync(
        session, isolated_project,
    )
    project_entries = [p for p in payload if p["scope"] == "project"]
    assert len(project_entries) >= 1
    assert project_entries[0]["scope_value"] is None

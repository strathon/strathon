"""Tests for audit/worker.py.

Two background loops:

- ``ensure_future_partitions`` — idempotent monthly partition DDL
- ``seal_anchor`` — per-interval Merkle root over events since last anchor

The loops themselves are tested by exercising one iteration each;
the asyncio.sleep / shutdown plumbing is identical to retention's
and exercised by retention's tests.
"""

from __future__ import annotations

import secrets
import uuid

import pytest
from sqlalchemy import text

import repositories.audit as audit_repo
from audit.actions import CATEGORY_POLICY, POLICY_CREATE
from audit.worker import ensure_future_partitions, seal_anchor


@pytest.fixture(autouse=True)
def _hmac_key(monkeypatch):
    monkeypatch.setenv("STRATHON_AUDIT_HMAC_KEY", secrets.token_hex(32))
    import config
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_ensure_future_partitions_idempotent(session):
    """Running twice does not error and produces the same partition set."""
    first = await ensure_future_partitions(session)
    second = await ensure_future_partitions(session)
    assert first == second
    # 4 entries: current + 3 lookahead.
    assert len(first) == 4
    for name in first:
        # Each should exist as a partition.
        result = await session.execute(
            text(
                "SELECT 1 FROM pg_inherits "
                "WHERE inhrelid = ('audit.' || :name)::regclass"
            ),
            {"name": name},
        )
        assert result.scalar_one_or_none() == 1


@pytest.mark.asyncio
async def test_ensure_future_partitions_naming(session):
    """Partition names follow events_YYYY_MM convention."""
    names = await ensure_future_partitions(session)
    for name in names:
        assert name.startswith("events_")
        parts = name.split("_")
        assert len(parts) == 3
        year_str, month_str = parts[1], parts[2]
        assert len(year_str) == 4 and year_str.isdigit()
        assert len(month_str) == 2 and month_str.isdigit()
        assert 1 <= int(month_str) <= 12


@pytest.mark.asyncio
async def test_seal_anchor_empty_interval_writes_no_anchor(async_engine):
    """When no events landed since last anchor, sealer writes nothing."""
    from sqlalchemy.ext.asyncio import AsyncSession

    # First drain anything since the last anchor.
    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        await seal_anchor(s)
        before = (
            await s.execute(text("SELECT COUNT(*) FROM audit.anchors"))
        ).scalar_one()
        summary = await seal_anchor(s)
        after = (
            await s.execute(text("SELECT COUNT(*) FROM audit.anchors"))
        ).scalar_one()

    assert summary["event_count"] == 0
    assert summary["merkle_root"] is None
    assert after == before


@pytest.mark.asyncio
async def test_seal_anchor_writes_for_nonempty_interval(async_engine):
    """When events exist since last anchor, sealer writes one row."""
    from sqlalchemy import insert
    from sqlalchemy.ext.asyncio import AsyncSession
    from models import Project, ProjectSettings

    # Fresh project + several events committed across sessions.
    pid = uuid.uuid4()
    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        await s.execute(
            insert(Project).values(id=pid, name=str(pid), slug=f"p-{pid.hex[:8]}")
        )
        await s.execute(insert(ProjectSettings).values(project_id=pid))
        await s.commit()

    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        for i in range(4):
            await audit_repo.emit(
                s, audit_repo.EmitContext.system(pid),
                POLICY_CREATE, CATEGORY_POLICY,
                resource_type="policy", resource_id=f"pol_{i}",
            )
        await s.commit()

    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        before = (
            await s.execute(text("SELECT COUNT(*) FROM audit.anchors"))
        ).scalar_one()
        summary = await seal_anchor(s)
        after = (
            await s.execute(text("SELECT COUNT(*) FROM audit.anchors"))
        ).scalar_one()

    assert summary["event_count"] >= 4
    assert summary["merkle_root"] is not None
    assert len(summary["merkle_root"]) == 32
    assert after == before + 1

    # Cleanup: drop the events we wrote and the anchor we created so
    # subsequent test runs see a clean slate. Disable the trigger,
    # delete, re-enable.
    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        for stmt in (
            "ALTER TABLE audit.events DISABLE TRIGGER events_no_delete",
            f"DELETE FROM audit.events WHERE project_id = '{pid}'",
            "ALTER TABLE audit.events ENABLE TRIGGER events_no_delete",
            f"DELETE FROM audit.anchors WHERE last_sequence >= 0 "
            f"AND event_count = {summary['event_count']}",
        ):
            await s.execute(text(stmt))
        await s.commit()


@pytest.mark.asyncio
async def test_seal_anchor_only_covers_new_events(async_engine):
    """A second seal after a first only captures events since the first."""
    from sqlalchemy import insert
    from sqlalchemy.ext.asyncio import AsyncSession
    from models import Project, ProjectSettings

    pid = uuid.uuid4()
    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        await s.execute(
            insert(Project).values(id=pid, name=str(pid), slug=f"p-{pid.hex[:8]}")
        )
        await s.execute(insert(ProjectSettings).values(project_id=pid))
        await s.commit()

    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        await audit_repo.emit(
            s, audit_repo.EmitContext.system(pid),
            POLICY_CREATE, CATEGORY_POLICY,
            resource_type="policy", resource_id="first",
        )
        await s.commit()

    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        summary1 = await seal_anchor(s)
    assert summary1["event_count"] >= 1

    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        await audit_repo.emit(
            s, audit_repo.EmitContext.system(pid),
            POLICY_CREATE, CATEGORY_POLICY,
            resource_type="policy", resource_id="second",
        )
        await s.commit()

    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        summary2 = await seal_anchor(s)
    # second anchor sees only the one new event for THIS project,
    # though other test data could also be in the interval.
    assert summary2["event_count"] >= 1
    assert summary2["last_sequence"] > summary1["last_sequence"]

    # Cleanup test data.
    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        await s.execute(text(
            "ALTER TABLE audit.events DISABLE TRIGGER events_no_delete"
        ))
        await s.execute(
            text("DELETE FROM audit.events WHERE project_id = :pid"),
            {"pid": pid},
        )
        await s.execute(text(
            "ALTER TABLE audit.events ENABLE TRIGGER events_no_delete"
        ))
        await s.commit()

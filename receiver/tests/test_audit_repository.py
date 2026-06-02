"""DB-backed tests for repositories/audit.py.

Exercises ``emit``, ``list_events``, ``get_event``, ``verify_event``,
stream CRUD, and the per-project advisory lock that serializes chain
writes. Uses the ``session`` and ``isolated_project`` fixtures from
the shared conftest, so each test runs in its own rolled-back
transaction.

The audit HMAC key is set per-test rather than at the session level
so we can verify the fail-closed and dev-fallback paths in isolation.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid

import pytest

import repositories.audit as audit_repo
from audit.actions import (
    CATEGORY_POLICY,
    OUTCOME_ALLOW,
    OUTCOME_DENY,
    POLICY_CREATE,
    POLICY_UPDATE,
)
from audit.hash_chain import GENESIS_PREV_HASH


@pytest.fixture(autouse=True)
def _ensure_hmac_key(monkeypatch):
    """Every test in this module needs an HMAC key set."""
    monkeypatch.setenv("STRATHON_AUDIT_HMAC_KEY", secrets.token_hex(32))
    # Clear cached Settings so the new env var is picked up.
    import config
    config.get_settings.cache_clear()
    # Also reset the warned-flag on the dev-fallback path so other tests
    # see fresh state if they reach it.
    try:
        del audit_repo._get_hmac_key._warned  # type: ignore[attr-defined]
    except AttributeError:
        pass
    yield
    config.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_emit_creates_event_with_genesis_prev_hash(
    session, isolated_project
):
    eid = await audit_repo.emit(
        session,
        audit_repo.EmitContext.system(isolated_project),
        POLICY_CREATE,
        CATEGORY_POLICY,
        resource_type="policy",
        resource_id="pol_1",
    )
    await session.flush()
    row = await audit_repo.get_event(session, isolated_project, eid)
    assert row is not None
    assert bytes(row["prev_hash"]) == GENESIS_PREV_HASH
    assert row["action"] == POLICY_CREATE
    assert row["action_category"] == CATEGORY_POLICY
    assert row["outcome"] == OUTCOME_ALLOW
    assert row["hmac_key_id"] == 1


@pytest.mark.asyncio
async def test_second_event_chains_to_first(session, isolated_project):
    ctx = audit_repo.EmitContext.system(isolated_project)
    eid1 = await audit_repo.emit(
        session, ctx, POLICY_CREATE, CATEGORY_POLICY,
        resource_type="policy", resource_id="pol_1",
    )
    await session.flush()
    eid2 = await audit_repo.emit(
        session, ctx, POLICY_UPDATE, CATEGORY_POLICY,
        resource_type="policy", resource_id="pol_1",
    )
    await session.flush()

    r1 = await audit_repo.get_event(session, isolated_project, eid1)
    r2 = await audit_repo.get_event(session, isolated_project, eid2)
    # Second row's prev_hash is the first row's row_hash.
    assert bytes(r2["prev_hash"]) == bytes(r1["row_hash"])
    assert r2["sequence_no"] > r1["sequence_no"]


@pytest.mark.asyncio
async def test_chains_are_per_project(session):
    """Two projects have independent chains; both start at genesis."""
    from sqlalchemy import insert
    from models import Project, ProjectSettings

    p1 = uuid.uuid4()
    p2 = uuid.uuid4()
    for pid in (p1, p2):
        await session.execute(
            insert(Project).values(org_id=__import__("uuid").UUID("00000000-0000-0000-0000-0000000000aa"), id=pid, name=str(pid), slug=f"p-{pid.hex[:8]}")
        )
        await session.execute(insert(ProjectSettings).values(project_id=pid))
    await session.flush()

    e1 = await audit_repo.emit(
        session, audit_repo.EmitContext.system(p1),
        POLICY_CREATE, CATEGORY_POLICY,
        resource_type="policy", resource_id="x",
    )
    e2 = await audit_repo.emit(
        session, audit_repo.EmitContext.system(p2),
        POLICY_CREATE, CATEGORY_POLICY,
        resource_type="policy", resource_id="y",
    )
    await session.flush()
    r1 = await audit_repo.get_event(session, p1, e1)
    r2 = await audit_repo.get_event(session, p2, e2)
    assert bytes(r1["prev_hash"]) == GENESIS_PREV_HASH
    assert bytes(r2["prev_hash"]) == GENESIS_PREV_HASH


@pytest.mark.asyncio
async def test_verify_event_returns_valid(session, isolated_project):
    eid = await audit_repo.emit(
        session, audit_repo.EmitContext.system(isolated_project),
        POLICY_CREATE, CATEGORY_POLICY,
        resource_type="policy", resource_id="pol_1",
        after_state={"name": "x", "priority": 10},
    )
    await session.flush()
    result = await audit_repo.verify_event(session, isolated_project, eid)
    assert result["valid"] is True
    assert result["event_id"] == str(eid)


@pytest.mark.asyncio
async def test_verify_event_returns_invalid_for_missing(
    session, isolated_project
):
    missing = uuid.uuid4()
    result = await audit_repo.verify_event(session, isolated_project, missing)
    assert result["valid"] is False
    assert result["error"] == "event_not_found"


@pytest.mark.asyncio
async def test_list_events_returns_newest_first(session, isolated_project):
    ctx = audit_repo.EmitContext.system(isolated_project)
    for i in range(3):
        await audit_repo.emit(
            session, ctx, POLICY_CREATE, CATEGORY_POLICY,
            resource_type="policy", resource_id=f"pol_{i}",
        )
        await session.flush()
    result = await audit_repo.list_events(session, isolated_project, limit=10)
    assert len(result.events) == 3
    seqs = [r["sequence_no"] for r in result.events]
    assert seqs == sorted(seqs, reverse=True)


@pytest.mark.asyncio
async def test_list_events_cursor_pagination(session, isolated_project):
    ctx = audit_repo.EmitContext.system(isolated_project)
    for i in range(5):
        await audit_repo.emit(
            session, ctx, POLICY_CREATE, CATEGORY_POLICY,
            resource_type="policy", resource_id=f"pol_{i}",
        )
        await session.flush()

    page1 = await audit_repo.list_events(session, isolated_project, limit=2)
    assert len(page1.events) == 2
    assert page1.next_cursor is not None

    page2 = await audit_repo.list_events(
        session, isolated_project, limit=2, cursor=page1.next_cursor,
    )
    assert len(page2.events) == 2
    # No overlap with page1.
    page1_ids = {r["id"] for r in page1.events}
    page2_ids = {r["id"] for r in page2.events}
    assert page1_ids.isdisjoint(page2_ids)

    page3 = await audit_repo.list_events(
        session, isolated_project, limit=2, cursor=page2.next_cursor,
    )
    assert len(page3.events) == 1  # 5 events, pages of 2 → 2+2+1
    assert page3.next_cursor is None


@pytest.mark.asyncio
async def test_list_events_invalid_cursor_raises(session, isolated_project):
    with pytest.raises(ValueError, match="invalid cursor"):
        await audit_repo.list_events(
            session, isolated_project, cursor="not-base64"
        )


@pytest.mark.asyncio
async def test_list_events_with_where_clause(session, isolated_project):
    """Filter on a column, only matching rows returned."""
    from audit.scim_filter import compile_to_sql

    ctx = audit_repo.EmitContext.system(isolated_project)
    await audit_repo.emit(
        session, ctx, POLICY_CREATE, CATEGORY_POLICY,
        resource_type="policy", resource_id="pol_1",
        outcome=OUTCOME_ALLOW,
    )
    await audit_repo.emit(
        session, ctx, POLICY_UPDATE, CATEGORY_POLICY,
        resource_type="policy", resource_id="pol_2",
        outcome=OUTCOME_DENY,
    )
    await session.flush()

    where, params = compile_to_sql('outcome eq "deny"')
    result = await audit_repo.list_events(
        session, isolated_project,
        where_clause=where, where_params=params,
    )
    assert len(result.events) == 1
    assert result.events[0]["outcome"] == OUTCOME_DENY


@pytest.mark.asyncio
async def test_diff_computed_from_before_after(session, isolated_project):
    eid = await audit_repo.emit(
        session, audit_repo.EmitContext.system(isolated_project),
        POLICY_UPDATE, CATEGORY_POLICY,
        resource_type="policy", resource_id="pol_1",
        before_state={"name": "old", "priority": 50},
        after_state={"name": "new", "priority": 100},
    )
    await session.flush()
    row = await audit_repo.get_event(session, isolated_project, eid)
    diff = row["diff"]
    assert diff is not None
    ops = {op["path"]: op for op in diff}
    assert ops["/name"]["op"] == "replace"
    assert ops["/name"]["value"] == "new"
    assert ops["/priority"]["op"] == "replace"


@pytest.mark.asyncio
async def test_redaction_applied_to_states(session, isolated_project):
    """Sensitive fields excluded from before/after at storage."""
    eid = await audit_repo.emit(
        session, audit_repo.EmitContext.system(isolated_project),
        POLICY_CREATE, CATEGORY_POLICY,
        resource_type="policy", resource_id="pol_1",
        after_state={
            "name": "x",
            "api_key": "sk_secret",
            "password": "p",
        },
    )
    await session.flush()
    row = await audit_repo.get_event(session, isolated_project, eid)
    after = row["after_state"]
    assert "api_key" not in after  # excluded
    assert after["password"] == "[REDACTED]"
    assert after["name"] == "x"


@pytest.mark.asyncio
async def test_advisory_lock_serializes_concurrent_writes(
    async_engine, isolated_project
):
    """Two concurrent emit calls produce a valid linear chain.

    Without the per-project advisory lock the two writers could read
    the same prev_hash and write rows with identical prev_hash and
    different row_hash values. With the lock they serialize.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    async def one_emit():
        async with AsyncSession(async_engine, expire_on_commit=False) as s:
            await audit_repo.emit(
                s, audit_repo.EmitContext.system(isolated_project),
                POLICY_CREATE, CATEGORY_POLICY,
                resource_type="policy", resource_id="concurrent",
            )
            await s.commit()

    await asyncio.gather(one_emit(), one_emit(), one_emit())

    # Read back. We expect 3 events forming an unbroken chain.
    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        result = await audit_repo.list_events(s, isolated_project, limit=10)
        rows = sorted(result.events, key=lambda r: r["sequence_no"])
        assert len(rows) == 3
        # Oldest links to genesis.
        assert bytes(rows[0]["prev_hash"]) == GENESIS_PREV_HASH
        # Each subsequent prev_hash equals the previous row_hash.
        for prev, curr in zip(rows, rows[1:]):
            assert bytes(curr["prev_hash"]) == bytes(prev["row_hash"])

        # Cleanup: delete the rows our cross-session writes left behind,
        # bypassing the append-only trigger via the same session that
        # would normally be locked down. We need to ALTER the trigger
        # because the rows survive the test-session rollback.
        from sqlalchemy import text as _t
        await s.execute(_t(
            "ALTER TABLE audit.events DISABLE TRIGGER events_no_delete"
        ))
        await s.execute(
            _t("DELETE FROM audit.events WHERE project_id = :pid"),
            {"pid": isolated_project},
        )
        await s.execute(_t(
            "ALTER TABLE audit.events ENABLE TRIGGER events_no_delete"
        ))
        await s.commit()


@pytest.mark.asyncio
async def test_stream_crud(session, isolated_project):
    """Create, list, delete an audit stream."""
    stream = await audit_repo.create_stream(
        session, isolated_project,
        name="my-stream",
        url="https://example.com/audit",
        categories=["policy"],
    )
    await session.flush()
    assert stream.name == "my-stream"

    listed = await audit_repo.list_streams(session, isolated_project)
    assert len(listed) == 1
    assert listed[0].id == stream.id

    deleted = await audit_repo.delete_stream(
        session, isolated_project, stream.id
    )
    assert deleted is True
    await session.flush()

    listed = await audit_repo.list_streams(session, isolated_project)
    assert listed == []


@pytest.mark.asyncio
async def test_delete_stream_returns_false_when_missing(
    session, isolated_project
):
    result = await audit_repo.delete_stream(
        session, isolated_project, uuid.uuid4()
    )
    assert result is False


@pytest.mark.asyncio
async def test_emit_fails_closed_with_empty_key_in_prod(
    monkeypatch, session, isolated_project
):
    """Empty STRATHON_AUDIT_HMAC_KEY in prod must refuse to emit."""
    monkeypatch.setenv("STRATHON_AUDIT_HMAC_KEY", "")
    monkeypatch.setenv("STRATHON_DEBUG", "false")
    import config
    config.get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="STRATHON_AUDIT_HMAC_KEY"):
        await audit_repo.emit(
            session, audit_repo.EmitContext.system(isolated_project),
            POLICY_CREATE, CATEGORY_POLICY,
            resource_type="policy", resource_id="x",
        )


@pytest.mark.asyncio
async def test_emit_rejects_short_key(
    monkeypatch, session, isolated_project
):
    """Key shorter than 32 bytes is rejected with a clear error."""
    monkeypatch.setenv("STRATHON_AUDIT_HMAC_KEY", "tooshort")
    import config
    config.get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        await audit_repo.emit(
            session, audit_repo.EmitContext.system(isolated_project),
            POLICY_CREATE, CATEGORY_POLICY,
            resource_type="policy", resource_id="x",
        )

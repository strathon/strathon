"""Tests for repositories/webhook_deliveries.py.

Cover the row-level CRUD and the sweeper's orphan-finder query without
going through the HTTP surface. HTTP-level tests live separately in
test_webhook_deliveries_api.py.

We deliberately keep tests narrow per concern (one assertion-cluster
per test) so a regression report tells the next engineer exactly
which behavior broke.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)

from sqlalchemy import insert  # noqa: E402

from models import Policy  # noqa: E402
from models.webhooks import WebhookDelivery  # noqa: E402
from repositories import webhook_deliveries as deliveries_repo  # noqa: E402


# ---- helpers ------------------------------------------------------------


async def _make_policy(session, project_id):
    pid = uuid.uuid4()
    await session.execute(insert(Policy).values(
        id=pid, project_id=project_id,
        name=f"deliv_policy_{pid.hex[:6]}",
        description="", match_expression="true",
        action="alert",
        action_config={"webhook_url": "https://example.test/hook"},
        applies_to=[], enabled=True, priority=0,
    ))
    await session.flush()
    return pid


async def _make_delivery(
    session, project_id, policy_id, *,
    status="pending", attempts=0, max_attempts=8,
    created_at=None, next_attempt_at=None,
    last_response_status=None, last_error=None,
):
    """Insert a delivery row with optional explicit timestamps for the
    sweeper tests. Returns the inserted row's id."""
    delivery_id = uuid.uuid4()
    values = dict(
        id=delivery_id,
        project_id=project_id,
        policy_id=policy_id,
        webhook_id=f"msg_test_{delivery_id.hex[:12]}",
        url="https://example.test/hook",
        payload={"event": "test"},
        status=status,
        attempts=attempts,
        max_attempts=max_attempts,
        last_response_status=last_response_status,
        last_error=last_error,
    )
    if created_at is not None:
        values["created_at"] = created_at
    if next_attempt_at is not None:
        values["next_attempt_at"] = next_attempt_at
    await session.execute(insert(WebhookDelivery).values(**values))
    await session.flush()
    return delivery_id


# ---- list_deliveries ----------------------------------------------------


async def test_list_returns_empty_for_empty_project(session, isolated_project):
    rows, cursor = await deliveries_repo.list_deliveries(session, isolated_project)
    assert rows == []
    assert cursor is None


async def test_list_returns_rows_newest_first(session, isolated_project):
    pid = await _make_policy(session, isolated_project)
    now = datetime.now(timezone.utc)
    older = await _make_delivery(
        session, isolated_project, pid,
        created_at=now - timedelta(minutes=5),
    )
    newer = await _make_delivery(
        session, isolated_project, pid,
        created_at=now,
    )
    rows, _ = await deliveries_repo.list_deliveries(session, isolated_project)
    assert [r.id for r in rows] == [newer, older]


async def test_list_filters_by_status(session, isolated_project):
    pid = await _make_policy(session, isolated_project)
    succ_id = await _make_delivery(session, isolated_project, pid, status="succeeded")
    await _make_delivery(session, isolated_project, pid, status="dlq")
    rows, _ = await deliveries_repo.list_deliveries(
        session, isolated_project, status="succeeded",
    )
    assert len(rows) == 1
    assert rows[0].id == succ_id


async def test_list_filters_by_policy_id(session, isolated_project):
    pid_a = await _make_policy(session, isolated_project)
    pid_b = await _make_policy(session, isolated_project)
    a_id = await _make_delivery(session, isolated_project, pid_a)
    await _make_delivery(session, isolated_project, pid_b)
    rows, _ = await deliveries_repo.list_deliveries(
        session, isolated_project, policy_id=pid_a,
    )
    assert len(rows) == 1
    assert rows[0].id == a_id


async def test_list_rejects_unknown_status(session, isolated_project):
    with pytest.raises(ValueError, match="unknown status"):
        await deliveries_repo.list_deliveries(
            session, isolated_project, status="totally_made_up",
        )


async def test_list_scopes_to_project(session, isolated_project, async_engine):
    """A delivery in project A must not appear in a list for project B."""
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import AsyncSession
    from models import Project, ProjectSettings

    other_pid = uuid.uuid4()
    async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
        await s.execute(insert(Project).values(
            org_id=__import__("uuid").UUID("00000000-0000-0000-0000-0000000000aa"), id=other_pid, name="other", slug=f"other-{other_pid.hex[:8]}",
        ))
        await s.execute(insert(ProjectSettings).values(project_id=other_pid))
        policy_in_other = await _make_policy(s, other_pid)
        await _make_delivery(s, other_pid, policy_in_other)
        await s.commit()

    try:
        rows, _ = await deliveries_repo.list_deliveries(session, isolated_project)
        # Test session sees zero — even though there's a delivery in the
        # other project, this list scopes to isolated_project.
        assert rows == []
    finally:
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            await s.execute(
                delete(WebhookDelivery).where(WebhookDelivery.project_id == other_pid)
            )
            await s.execute(delete(Policy).where(Policy.project_id == other_pid))
            await s.execute(
                delete(ProjectSettings).where(ProjectSettings.project_id == other_pid)
            )
            await s.execute(delete(Project).where(Project.id == other_pid))
            await s.commit()


async def test_list_paginates_with_cursor(session, isolated_project):
    """Cursor pagination: first page returns next_cursor when there's
    a next page, follow-up call with that cursor returns the rest."""
    pid = await _make_policy(session, isolated_project)
    base = datetime.now(timezone.utc)
    # Insert 5 deliveries with distinct created_at so ordering is stable
    ids = []
    for i in range(5):
        d = await _make_delivery(
            session, isolated_project, pid,
            created_at=base - timedelta(seconds=i),
        )
        ids.append(d)

    page1, next_cursor = await deliveries_repo.list_deliveries(
        session, isolated_project, limit=2,
    )
    assert len(page1) == 2
    assert next_cursor is not None
    # Newest two
    assert [r.id for r in page1] == [ids[0], ids[1]]

    page2, cursor2 = await deliveries_repo.list_deliveries(
        session, isolated_project, limit=2, cursor=next_cursor,
    )
    assert len(page2) == 2
    assert [r.id for r in page2] == [ids[2], ids[3]]
    assert cursor2 is not None

    page3, cursor3 = await deliveries_repo.list_deliveries(
        session, isolated_project, limit=2, cursor=cursor2,
    )
    assert len(page3) == 1
    assert page3[0].id == ids[4]
    assert cursor3 is None  # last page


async def test_list_limit_capped_at_200(session, isolated_project):
    """A caller asking for limit=500 doesn't get more than 200."""
    pid = await _make_policy(session, isolated_project)
    for _ in range(3):
        await _make_delivery(session, isolated_project, pid)
    rows, _ = await deliveries_repo.list_deliveries(
        session, isolated_project, limit=500,
    )
    # Only 3 exist; verify the call returns them without raising about the
    # over-limit ask (the cap is silent).
    assert len(rows) == 3


async def test_list_rejects_invalid_cursor(session, isolated_project):
    with pytest.raises(ValueError, match="invalid cursor"):
        await deliveries_repo.list_deliveries(
            session, isolated_project, cursor="not-base64-at-all",
        )


# ---- get_delivery -------------------------------------------------------


async def test_get_delivery_returns_full_payload(session, isolated_project):
    pid = await _make_policy(session, isolated_project)
    did = await _make_delivery(session, isolated_project, pid)
    got = await deliveries_repo.get_delivery(session, did, isolated_project)
    assert got is not None
    assert got["id"] == str(did)
    assert "payload" in got
    assert got["payload"] == {"event": "test"}


async def test_get_delivery_unknown_id_returns_none(session, isolated_project):
    got = await deliveries_repo.get_delivery(session, uuid.uuid4(), isolated_project)
    assert got is None


async def test_get_delivery_wrong_project_returns_none(
    session, isolated_project, async_engine,
):
    """A delivery in project A is invisible to a get from project B —
    not 404'd differently, the function returns None and the API layer
    converts to 404 without leaking existence info."""
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import AsyncSession
    from models import Project, ProjectSettings

    other_pid = uuid.uuid4()
    other_did = None
    async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
        await s.execute(insert(Project).values(
            org_id=__import__("uuid").UUID("00000000-0000-0000-0000-0000000000aa"), id=other_pid, name="other", slug=f"other-{other_pid.hex[:8]}",
        ))
        await s.execute(insert(ProjectSettings).values(project_id=other_pid))
        policy_in_other = await _make_policy(s, other_pid)
        other_did = await _make_delivery(s, other_pid, policy_in_other)
        await s.commit()

    try:
        got = await deliveries_repo.get_delivery(
            session, other_did, isolated_project,
        )
        assert got is None
    finally:
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            await s.execute(
                delete(WebhookDelivery).where(WebhookDelivery.project_id == other_pid)
            )
            await s.execute(delete(Policy).where(Policy.project_id == other_pid))
            await s.execute(
                delete(ProjectSettings).where(ProjectSettings.project_id == other_pid)
            )
            await s.execute(delete(Project).where(Project.id == other_pid))
            await s.commit()


# ---- replay_delivery ----------------------------------------------------


async def test_replay_dlq_resets_attempts_and_status(session, isolated_project):
    pid = await _make_policy(session, isolated_project)
    did = await _make_delivery(
        session, isolated_project, pid,
        status="dlq", attempts=8,
        last_response_status=503, last_error="upstream down",
    )
    updated = await deliveries_repo.replay_delivery(session, did, isolated_project)
    assert updated is not None
    assert updated.status == "pending"
    assert updated.attempts == 0
    assert updated.last_response_status is None
    assert updated.last_error is None


async def test_replay_abandoned_is_allowed(session, isolated_project):
    pid = await _make_policy(session, isolated_project)
    did = await _make_delivery(
        session, isolated_project, pid,
        status="abandoned", attempts=1,
        last_response_status=404,
    )
    updated = await deliveries_repo.replay_delivery(session, did, isolated_project)
    assert updated.status == "pending"


async def test_replay_succeeded_raises(session, isolated_project):
    """Replaying a successful delivery is a future feature; for v1 the
    repo raises so the API can return 409."""
    pid = await _make_policy(session, isolated_project)
    did = await _make_delivery(
        session, isolated_project, pid, status="succeeded", attempts=1,
    )
    with pytest.raises(ValueError, match="replay is only allowed"):
        await deliveries_repo.replay_delivery(session, did, isolated_project)


async def test_replay_pending_raises(session, isolated_project):
    """Replaying a row already pending would race the retry middleware."""
    pid = await _make_policy(session, isolated_project)
    did = await _make_delivery(session, isolated_project, pid, status="pending")
    with pytest.raises(ValueError):
        await deliveries_repo.replay_delivery(session, did, isolated_project)


async def test_replay_unknown_id_returns_none(session, isolated_project):
    got = await deliveries_repo.replay_delivery(
        session, uuid.uuid4(), isolated_project,
    )
    assert got is None


# ---- find_orphan_pending_deliveries (sweeper query) --------------------


async def test_sweeper_finds_pending_older_than_threshold(session, isolated_project):
    """The sweeper picks up the stale orphan but not the fresh one.

    Like the terminal-statuses test, we don't assert on the total length
    of the returned list since the sweeper query is global and other
    tests in this run may have committed orphan rows we don't know about.
    """
    pid = await _make_policy(session, isolated_project)
    now = datetime.now(timezone.utc)
    orphan = await _make_delivery(
        session, isolated_project, pid,
        status="pending",
        next_attempt_at=now - timedelta(minutes=10),
    )
    fresh = await _make_delivery(
        session, isolated_project, pid,
        status="pending",
        next_attempt_at=now,
    )
    ids = await deliveries_repo.find_orphan_pending_deliveries(
        session, threshold_seconds=300,  # 5 min
    )
    assert orphan in ids
    assert fresh not in ids


async def test_sweeper_ignores_terminal_statuses(session, isolated_project):
    """A DLQ row from years ago should NOT show up in the sweeper's list —
    those are reclaimed via manual replay, not the auto-sweeper.

    The sweeper query is intentionally global (it's a daemon that
    re-dispatches across all projects). To make this test independent
    of whatever rows other tests committed to the default project, we
    only assert the IDs we just inserted are absent from the result.
    """
    pid = await _make_policy(session, isolated_project)
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    dlq_id = await _make_delivery(
        session, isolated_project, pid,
        status="dlq",
        next_attempt_at=long_ago,
    )
    succ_id = await _make_delivery(
        session, isolated_project, pid,
        status="succeeded",
        next_attempt_at=long_ago,
    )
    aband_id = await _make_delivery(
        session, isolated_project, pid,
        status="abandoned",
        next_attempt_at=long_ago,
    )
    ids = await deliveries_repo.find_orphan_pending_deliveries(
        session, threshold_seconds=60,
    )
    # None of our terminal-status rows should appear in the orphan list.
    assert dlq_id not in ids
    assert succ_id not in ids
    assert aband_id not in ids


async def test_sweeper_respects_limit(session, isolated_project):
    pid = await _make_policy(session, isolated_project)
    long_ago = datetime.now(timezone.utc) - timedelta(minutes=10)
    for _ in range(5):
        await _make_delivery(
            session, isolated_project, pid,
            status="pending", next_attempt_at=long_ago,
        )
    ids = await deliveries_repo.find_orphan_pending_deliveries(
        session, threshold_seconds=60, limit=3,
    )
    assert len(ids) == 3

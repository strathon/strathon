"""Tests for webhooks.dispatch.

The dispatch layer's job is narrow:

  * Insert a webhook_deliveries row with the right shape inside the
    caller's transaction (atomicity with policy_matches).
  * Generate a webhook_id following the Standard Webhooks 'msg_*'
    convention so consumers can use it as an idempotency key.
  * Register an after_commit hook so the Dramatiq message is sent only
    after the row is durable — no phantom deliveries.

We don't exercise actual HTTP sends here (that's the actor's job). We
do verify that the Dramatiq send is called exactly when we want it to
be: after a successful commit, never after a rollback.
"""

from __future__ import annotations

import os
import sys
import uuid
from unittest.mock import patch

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)

from sqlalchemy import select  # noqa: E402

from models.webhooks import WebhookDelivery  # noqa: E402
from webhooks.dispatch import enqueue_delivery  # noqa: E402


# ---- Helpers ------------------------------------------------------------


async def _make_policy(session, project_id):
    """Create a minimal alert policy attached to the project so we can
    use its id as the FK in webhook_deliveries.policy_id."""
    from sqlalchemy import insert
    from models import Policy

    policy_id = uuid.uuid4()
    await session.execute(
        insert(Policy).values(
            id=policy_id,
            project_id=project_id,
            name=f"test_alert_{policy_id.hex[:6]}",
            description="dispatch test alert",
            match_expression="true",
            action="alert",
            action_config={"webhook_url": "https://example.test/hook"},
            applies_to=[],
            enabled=True,
            priority=0,
        )
    )
    await session.flush()
    return policy_id


# ---- Row shape ----------------------------------------------------------


async def test_enqueue_delivery_inserts_pending_row(session, isolated_project):
    policy_id = await _make_policy(session, isolated_project)

    delivery = await enqueue_delivery(
        session,
        project_id=isolated_project,
        policy_id=policy_id,
        url="https://example.test/hook",
        payload={"event": "test"},
    )

    fetched = await session.scalar(
        select(WebhookDelivery).where(WebhookDelivery.id == delivery.id)
    )
    assert fetched is not None
    assert fetched.status == "pending"
    assert fetched.attempts == 0
    assert fetched.max_attempts == 8  # the server-default value
    assert fetched.url == "https://example.test/hook"
    assert fetched.payload == {"event": "test"}
    assert fetched.project_id == isolated_project
    assert fetched.policy_id == policy_id


async def test_enqueue_delivery_generates_webhook_id_with_msg_prefix(
    session, isolated_project,
):
    """The Standard Webhooks 'msg_' prefix on the webhook-id header is
    the convention every consumer library expects."""
    policy_id = await _make_policy(session, isolated_project)
    delivery = await enqueue_delivery(
        session,
        project_id=isolated_project, policy_id=policy_id,
        url="https://example.test/hook", payload={},
    )
    assert delivery.webhook_id.startswith("msg_")
    # The 16 random url-safe bytes encode to 22 chars; total length ~26
    assert 20 <= len(delivery.webhook_id) <= 30


async def test_enqueue_delivery_generates_unique_webhook_ids(
    session, isolated_project,
):
    """Two enqueues must produce distinct ids; this catches a regression
    where token_urlsafe() somehow got seeded deterministically."""
    policy_id = await _make_policy(session, isolated_project)
    d1 = await enqueue_delivery(
        session, project_id=isolated_project, policy_id=policy_id,
        url="https://example.test/h1", payload={},
    )
    d2 = await enqueue_delivery(
        session, project_id=isolated_project, policy_id=policy_id,
        url="https://example.test/h2", payload={},
    )
    assert d1.webhook_id != d2.webhook_id
    assert d1.id != d2.id


async def test_enqueue_delivery_rejects_empty_url(session, isolated_project):
    policy_id = await _make_policy(session, isolated_project)
    with pytest.raises(ValueError, match="non-empty url"):
        await enqueue_delivery(
            session,
            project_id=isolated_project, policy_id=policy_id,
            url="", payload={"x": 1},
        )


# ---- after_commit hook --------------------------------------------------


async def test_dramatiq_send_does_not_fire_before_commit(
    session, isolated_project,
):
    """The whole point of the after_commit hook: a Dramatiq send before
    commit could leave a phantom delivery for a rolled-back row.

    The test session rolls back at teardown (no commit ever happens) so
    we should see ZERO sends recorded — if any send happens, the
    architecture's atomicity guarantee is broken.
    """
    policy_id = await _make_policy(session, isolated_project)

    with patch("webhooks.dispatch._send_dramatiq_message") as send_mock:
        await enqueue_delivery(
            session,
            project_id=isolated_project, policy_id=policy_id,
            url="https://example.test/hook", payload={},
        )
        # No commit has happened, no rollback either — but in this test
        # framework the session ends with rollback. Assert nothing has
        # been sent.
        assert send_mock.call_count == 0


async def test_dramatiq_send_fires_exactly_once_after_commit(async_engine):
    """Open NEW sessions that we control commits on, so we can prove
    the send fires exactly once after a real commit (not after a
    rollback, not before).

    Because we need a committed project for the FK to be satisfied, we
    create+commit the project + policy ourselves, then clean them up
    at the end so subsequent tests see a clean DB.
    """
    from sqlalchemy import delete, insert
    from sqlalchemy.ext.asyncio import AsyncSession

    from models import Policy, Project, ProjectSettings
    from models.webhooks import WebhookDelivery

    project_id = uuid.uuid4()
    policy_id = uuid.uuid4()
    slug = f"after-commit-{project_id.hex[:8]}"

    # Set up committed project + policy.
    async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
        await s.execute(insert(Project).values(
            org_id=__import__("uuid").UUID("00000000-0000-0000-0000-0000000000aa"), id=project_id, name=slug, slug=slug,
        ))
        await s.execute(insert(ProjectSettings).values(project_id=project_id))
        await s.execute(insert(Policy).values(
            id=policy_id, project_id=project_id,
            name=f"after_commit_{policy_id.hex[:6]}",
            description="", match_expression="true",
            action="alert",
            action_config={"webhook_url": "https://example.test/hook"},
            applies_to=[], enabled=True, priority=0,
        ))
        await s.commit()

    try:
        with patch("webhooks.dispatch._send_dramatiq_message") as send_mock:
            async with AsyncSession(bind=async_engine, expire_on_commit=False) as s2:
                delivery = await enqueue_delivery(
                    s2,
                    project_id=project_id, policy_id=policy_id,
                    url="https://example.test/hook", payload={"k": "v"},
                )
                delivery_id = str(delivery.id)
                assert send_mock.call_count == 0
                await s2.commit()
            assert send_mock.call_count == 1
            assert send_mock.call_args.args == (delivery_id,)
    finally:
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as cleanup:
            await cleanup.execute(
                delete(WebhookDelivery).where(WebhookDelivery.policy_id == policy_id)
            )
            await cleanup.execute(delete(Policy).where(Policy.id == policy_id))
            await cleanup.execute(
                delete(ProjectSettings).where(ProjectSettings.project_id == project_id)
            )
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.commit()


async def test_dramatiq_send_does_not_fire_on_rollback(async_engine):
    """The mirror of the commit-fires-once test: a transaction that
    rolls back must produce zero sends."""
    from sqlalchemy import delete, insert
    from sqlalchemy.ext.asyncio import AsyncSession

    from models import Policy, Project, ProjectSettings

    project_id = uuid.uuid4()
    policy_id = uuid.uuid4()
    slug = f"rollback-{project_id.hex[:8]}"

    async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
        await s.execute(insert(Project).values(
            org_id=__import__("uuid").UUID("00000000-0000-0000-0000-0000000000aa"), id=project_id, name=slug, slug=slug,
        ))
        await s.execute(insert(ProjectSettings).values(project_id=project_id))
        await s.execute(insert(Policy).values(
            id=policy_id, project_id=project_id,
            name=f"rollback_{policy_id.hex[:6]}",
            description="", match_expression="true",
            action="alert",
            action_config={"webhook_url": "https://example.test/hook"},
            applies_to=[], enabled=True, priority=0,
        ))
        await s.commit()

    try:
        with patch("webhooks.dispatch._send_dramatiq_message") as send_mock:
            async with AsyncSession(bind=async_engine, expire_on_commit=False) as s2:
                await enqueue_delivery(
                    s2,
                    project_id=project_id, policy_id=policy_id,
                    url="https://example.test/hook", payload={},
                )
                await s2.rollback()
            assert send_mock.call_count == 0
    finally:
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as cleanup:
            await cleanup.execute(delete(Policy).where(Policy.id == policy_id))
            await cleanup.execute(
                delete(ProjectSettings).where(ProjectSettings.project_id == project_id)
            )
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.commit()

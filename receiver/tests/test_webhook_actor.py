"""Tests for webhooks.actor.

These tests exercise the actor's classification rules (which response
codes get retried vs abandoned vs succeeded) and its DB-update behavior
on each branch. We do NOT run the actor through real Dramatiq workers
here — Dramatiq's retry middleware is well-tested upstream; what we own
is the classification logic and the response handling.

Approach
========

The actor's HTTP layer is httpx; we mock it via httpx.MockTransport so
each test pins the response the actor sees. Each test then asserts:

  * The webhook_deliveries row was updated to the expected status
  * attempts was incremented by 1
  * last_response_status reflects what we mocked
  * Retriable failures raised the expected exception (Dramatiq's retry
    middleware will see this and schedule a backoff retry in production)
  * Non-retriable failures returned cleanly without raising
  * A row already in a terminal state is a no-op (idempotency)
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone

import httpx
import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)

from sqlalchemy import select, insert, delete  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from models import Policy, Project, ProjectSettings  # noqa: E402
from models.webhooks import WebhookDelivery  # noqa: E402
from webhooks.actor import _RetriableDeliveryError, _send_one  # noqa: E402


# ---- helpers ------------------------------------------------------------


async def _committed_project_and_policy(async_engine):
    """Create a project + alert policy in a separate committed
    transaction so FKs are satisfiable for tests that need to verify
    real row updates.

    Returns (project_id, policy_id) and a cleanup callable.
    """
    project_id = uuid.uuid4()
    policy_id = uuid.uuid4()
    slug = f"actor-{project_id.hex[:8]}"

    async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
        await s.execute(insert(Project).values(org_id=__import__("uuid").UUID("00000000-0000-0000-0000-0000000000aa"), id=project_id, name=slug, slug=slug))
        await s.execute(insert(ProjectSettings).values(project_id=project_id))
        await s.execute(insert(Policy).values(
            id=policy_id, project_id=project_id,
            name=f"actor_policy_{policy_id.hex[:6]}",
            description="", match_expression="true",
            action="alert",
            action_config={"webhook_url": "https://example.test/hook"},
            applies_to=[], enabled=True, priority=0,
        ))
        await s.commit()

    async def cleanup():
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            await s.execute(
                delete(WebhookDelivery).where(WebhookDelivery.policy_id == policy_id)
            )
            await s.execute(delete(Policy).where(Policy.id == policy_id))
            await s.execute(
                delete(ProjectSettings).where(ProjectSettings.project_id == project_id)
            )
            await s.execute(delete(Project).where(Project.id == project_id))
            await s.commit()

    return project_id, policy_id, cleanup


async def _insert_pending_delivery(
    async_engine, project_id, policy_id, *,
    max_attempts=8, attempts=0,
):
    """Commit a pending delivery and return its id as a string."""
    delivery_id = uuid.uuid4()
    async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
        await s.execute(insert(WebhookDelivery).values(
            id=delivery_id,
            project_id=project_id, policy_id=policy_id,
            webhook_id=f"msg_test_{delivery_id.hex[:12]}",
            url="https://example.test/hook",
            payload={"event": "test"},
            status="pending",
            attempts=attempts,
            max_attempts=max_attempts,
            next_attempt_at=datetime.now(timezone.utc),
        ))
        await s.commit()
    return str(delivery_id)


# Monkeypatch helper: install an httpx mock for the duration of one
# _send_one call by patching httpx.AsyncClient.
class _PatchedAsyncClient:
    def __init__(self, transport):
        self._transport = transport
    async def __aenter__(self):
        return self._client
    async def __aexit__(self, *args):
        await self._client.aclose()
    def __enter__(self):
        # Replace httpx.AsyncClient with a factory that returns one
        # bound to our mock transport. We monkeypatch the symbol that
        # webhooks.actor imports.
        import webhooks.actor as actor_mod
        self._original = actor_mod.httpx
        # Build a small shim that has AsyncClient(...) returning our pinned client
        class _Shim:
            TimeoutException = httpx.TimeoutException
            ConnectError = httpx.ConnectError
            HTTPError = httpx.HTTPError
            def AsyncClient(self_shim, timeout):
                return httpx.AsyncClient(transport=self._transport, timeout=timeout)
        actor_mod.httpx = _Shim()
        return self
    def __exit__(self, *args):
        import webhooks.actor as actor_mod
        actor_mod.httpx = self._original


def _mock_transport_returning(status_code: int):
    def handler(request):
        return httpx.Response(status_code)
    return httpx.MockTransport(handler)


def _mock_transport_timing_out():
    def handler(request):
        raise httpx.TimeoutException("simulated timeout")
    return httpx.MockTransport(handler)


def _mock_transport_conn_refused():
    def handler(request):
        raise httpx.ConnectError("simulated conn refused")
    return httpx.MockTransport(handler)


# ---- 2xx -> succeeded ---------------------------------------------------


async def test_2xx_response_marks_delivery_succeeded(async_engine):
    project_id, policy_id, cleanup = await _committed_project_and_policy(async_engine)
    delivery_id = await _insert_pending_delivery(async_engine, project_id, policy_id)
    try:
        with _PatchedAsyncClient(_mock_transport_returning(200)):
            async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
                result = await _send_one(s, delivery_id, request_timeout_sec=5.0)
        assert result == "succeeded"

        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            row = await s.scalar(
                select(WebhookDelivery).where(WebhookDelivery.id == uuid.UUID(delivery_id))
            )
            assert row.status == "succeeded"
            assert row.attempts == 1
            assert row.last_response_status == 200
            assert row.last_error is None
            assert row.last_attempt_at is not None
    finally:
        await cleanup()


# ---- 5xx -> retriable, status=failed_retrying, exception raised ---------


async def test_5xx_raises_retriable_and_marks_failed_retrying(async_engine):
    project_id, policy_id, cleanup = await _committed_project_and_policy(async_engine)
    delivery_id = await _insert_pending_delivery(
        async_engine, project_id, policy_id, max_attempts=5,
    )
    try:
        with _PatchedAsyncClient(_mock_transport_returning(503)):
            async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
                with pytest.raises(_RetriableDeliveryError):
                    await _send_one(s, delivery_id, request_timeout_sec=5.0)

        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            row = await s.scalar(
                select(WebhookDelivery).where(WebhookDelivery.id == uuid.UUID(delivery_id))
            )
            # First attempt — still 4 retries left under max_attempts=5
            assert row.status == "failed_retrying"
            assert row.attempts == 1
            assert row.last_response_status == 503
    finally:
        await cleanup()


# ---- 5xx on last attempt -> dlq, no exception (no more retries) --------


async def test_5xx_on_last_attempt_flips_to_dlq(async_engine):
    project_id, policy_id, cleanup = await _committed_project_and_policy(async_engine)
    # attempts=4, max_attempts=5: this attempt is the last one
    delivery_id = await _insert_pending_delivery(
        async_engine, project_id, policy_id,
        attempts=4, max_attempts=5,
    )
    try:
        with _PatchedAsyncClient(_mock_transport_returning(503)):
            async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
                # When we reach DLQ we should NOT raise — Dramatiq has no
                # more retries to schedule, so raising would just spam
                # the error logs without changing outcome.
                result = await _send_one(s, delivery_id, request_timeout_sec=5.0)
        assert result == "dlq"

        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            row = await s.scalar(
                select(WebhookDelivery).where(WebhookDelivery.id == uuid.UUID(delivery_id))
            )
            assert row.status == "dlq"
            assert row.attempts == 5
            assert row.attempts == row.max_attempts
    finally:
        await cleanup()


# ---- 4xx (non-429) -> abandoned, no exception ---------------------------


async def test_404_marks_abandoned_no_retry(async_engine):
    """A 404 means the URL is wrong; retrying won't fix it. Abandon."""
    project_id, policy_id, cleanup = await _committed_project_and_policy(async_engine)
    delivery_id = await _insert_pending_delivery(async_engine, project_id, policy_id)
    try:
        with _PatchedAsyncClient(_mock_transport_returning(404)):
            async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
                result = await _send_one(s, delivery_id, request_timeout_sec=5.0)
        assert result == "abandoned"
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            row = await s.scalar(
                select(WebhookDelivery).where(WebhookDelivery.id == uuid.UUID(delivery_id))
            )
            assert row.status == "abandoned"
            assert row.last_response_status == 404
    finally:
        await cleanup()


async def test_400_marks_abandoned(async_engine):
    project_id, policy_id, cleanup = await _committed_project_and_policy(async_engine)
    delivery_id = await _insert_pending_delivery(async_engine, project_id, policy_id)
    try:
        with _PatchedAsyncClient(_mock_transport_returning(400)):
            async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
                result = await _send_one(s, delivery_id, request_timeout_sec=5.0)
        assert result == "abandoned"
    finally:
        await cleanup()


# ---- 429 -> retriable (rate-limited) -----------------------------------


async def test_429_marks_failed_retrying_like_5xx(async_engine):
    """429 is the one 4xx we DO retry — it explicitly says 'come back later'."""
    project_id, policy_id, cleanup = await _committed_project_and_policy(async_engine)
    delivery_id = await _insert_pending_delivery(async_engine, project_id, policy_id)
    try:
        with _PatchedAsyncClient(_mock_transport_returning(429)):
            async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
                with pytest.raises(_RetriableDeliveryError):
                    await _send_one(s, delivery_id, request_timeout_sec=5.0)
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            row = await s.scalar(
                select(WebhookDelivery).where(WebhookDelivery.id == uuid.UUID(delivery_id))
            )
            assert row.status == "failed_retrying"
            assert row.last_response_status == 429
    finally:
        await cleanup()


# ---- 3xx -> abandoned (redirects are a known webhook security hole) -----


async def test_301_marks_abandoned(async_engine):
    """Following redirects in webhook delivery opens security holes
    (the redirect URL could route to anywhere, and signature verification
    semantics get confused). Treat 3xx as terminal."""
    project_id, policy_id, cleanup = await _committed_project_and_policy(async_engine)
    delivery_id = await _insert_pending_delivery(async_engine, project_id, policy_id)
    try:
        with _PatchedAsyncClient(_mock_transport_returning(301)):
            async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
                result = await _send_one(s, delivery_id, request_timeout_sec=5.0)
        assert result == "abandoned"
    finally:
        await cleanup()


# ---- Timeout -> retriable ----------------------------------------------


async def test_timeout_raises_retriable(async_engine):
    project_id, policy_id, cleanup = await _committed_project_and_policy(async_engine)
    delivery_id = await _insert_pending_delivery(async_engine, project_id, policy_id)
    try:
        with _PatchedAsyncClient(_mock_transport_timing_out()):
            async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
                with pytest.raises(_RetriableDeliveryError):
                    await _send_one(s, delivery_id, request_timeout_sec=5.0)
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            row = await s.scalar(
                select(WebhookDelivery).where(WebhookDelivery.id == uuid.UUID(delivery_id))
            )
            assert row.status == "failed_retrying"
            assert row.last_response_status is None  # never got a response
            assert "TimeoutException" in (row.last_error or "")
    finally:
        await cleanup()


async def test_connection_refused_raises_retriable(async_engine):
    project_id, policy_id, cleanup = await _committed_project_and_policy(async_engine)
    delivery_id = await _insert_pending_delivery(async_engine, project_id, policy_id)
    try:
        with _PatchedAsyncClient(_mock_transport_conn_refused()):
            async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
                with pytest.raises(_RetriableDeliveryError):
                    await _send_one(s, delivery_id, request_timeout_sec=5.0)
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            row = await s.scalar(
                select(WebhookDelivery).where(WebhookDelivery.id == uuid.UUID(delivery_id))
            )
            assert row.status == "failed_retrying"
            assert "ConnectError" in (row.last_error or "")
    finally:
        await cleanup()


# ---- Idempotency: terminal rows are no-op ------------------------------


async def test_already_succeeded_row_is_noop(async_engine):
    """A duplicate enqueue (or sweeper race) must not re-fire a delivery
    that's already terminal. The actor returns the current status without
    making an HTTP call."""
    project_id, policy_id, cleanup = await _committed_project_and_policy(async_engine)
    delivery_id = await _insert_pending_delivery(async_engine, project_id, policy_id)
    # Flip to succeeded directly
    async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
        row = await s.scalar(
            select(WebhookDelivery).where(WebhookDelivery.id == uuid.UUID(delivery_id))
        )
        row.status = "succeeded"
        row.attempts = 3
        await s.commit()

    try:
        # We don't install an HTTP mock — if the actor tries to make an
        # HTTP request it'll fail with a real connection error and the
        # test will fail. That's the assertion: terminal rows skip HTTP.
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            result = await _send_one(s, delivery_id, request_timeout_sec=5.0)
        assert result == "succeeded"

        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            row = await s.scalar(
                select(WebhookDelivery).where(WebhookDelivery.id == uuid.UUID(delivery_id))
            )
            # Attempts unchanged — no HTTP call was made
            assert row.attempts == 3
    finally:
        await cleanup()


async def test_already_dlq_row_is_noop(async_engine):
    project_id, policy_id, cleanup = await _committed_project_and_policy(async_engine)
    delivery_id = await _insert_pending_delivery(
        async_engine, project_id, policy_id, attempts=8, max_attempts=8,
    )
    async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
        row = await s.scalar(
            select(WebhookDelivery).where(WebhookDelivery.id == uuid.UUID(delivery_id))
        )
        row.status = "dlq"
        await s.commit()

    try:
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            result = await _send_one(s, delivery_id, request_timeout_sec=5.0)
        assert result == "dlq"
    finally:
        await cleanup()


async def test_missing_delivery_id_returns_cleanly(async_engine):
    """If the actor is invoked with a deleted/unknown id (sweeper race
    with retention cleanup, say), it must return cleanly — not raise."""
    fake_id = str(uuid.uuid4())
    async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
        result = await _send_one(s, fake_id, request_timeout_sec=5.0)
    assert result == "missing"

"""Tests for webhooks.sweeper.

Three layers:
  * SweeperConfig.from_env — config parsing with autouse fixture
    that cleans cached Settings (same trap as test_retention.py).
  * sweep_once — single-tick behavior: counts dispatches, no-op when
    nothing's orphaned, doesn't crash if Dramatiq is unavailable.
  * sweeper_loop — lifecycle: respects shutdown event, recovers from
    per-tick exceptions, can be disabled via config.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)

from sqlalchemy import insert  # noqa: E402

from models import Policy  # noqa: E402
from models.webhooks import WebhookDelivery  # noqa: E402
from webhooks.sweeper import (  # noqa: E402
    DEFAULT_BATCH_LIMIT,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_THRESHOLD_SECONDS,
    SweeperConfig,
    sweep_once,
    sweeper_loop,
)


@pytest.fixture(autouse=True)
def _reset_settings_cache_after_test():
    """Same trap as test_retention.py — config-parsing tests that use
    patch.dict(env, clear=True) leave the lru_cache in config polluted
    with whatever DATABASE_URL the patched env had. Clean after each
    test to keep other modules' tests independent."""
    yield
    try:
        import config as cfg_mod
        try:
            cfg_mod.get_settings.cache_clear()
        except Exception:
            pass
        importlib.reload(cfg_mod)
    except Exception:
        pass


# ---- SweeperConfig.from_env --------------------------------------------


def test_config_defaults_when_env_empty():
    with patch.dict(
        os.environ,
        {"DATABASE_URL": "postgresql://x:x@127.0.0.1:5432/x"},
        clear=True,
    ):
        cfg = SweeperConfig.from_env()
    assert cfg.enabled is True
    assert cfg.interval_seconds == DEFAULT_INTERVAL_SECONDS
    assert cfg.threshold_seconds == DEFAULT_THRESHOLD_SECONDS
    assert cfg.batch_limit == DEFAULT_BATCH_LIMIT


def test_config_disabled_via_env():
    for val in ("false", "False", "0", "no", "off"):
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://x:x@127.0.0.1:5432/x",
                "STRATHON_WEBHOOK_SWEEPER_ENABLED": val,
            },
            clear=True,
        ):
            cfg = SweeperConfig.from_env()
            assert cfg.enabled is False, f"failed for {val!r}"


def test_config_respects_interval_threshold_batch():
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": "postgresql://x:x@127.0.0.1:5432/x",
            "STRATHON_WEBHOOK_SWEEPER_INTERVAL_SEC": "30",
            "STRATHON_WEBHOOK_SWEEPER_THRESHOLD_SEC": "120",
            "STRATHON_WEBHOOK_SWEEPER_BATCH": "25",
        },
        clear=True,
    ):
        cfg = SweeperConfig.from_env()
    assert cfg.interval_seconds == 30
    assert cfg.threshold_seconds == 120
    assert cfg.batch_limit == 25


def test_config_falls_back_to_default_for_non_int_env():
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": "postgresql://x:x@127.0.0.1:5432/x",
            "STRATHON_WEBHOOK_SWEEPER_INTERVAL_SEC": "not-a-number",
        },
        clear=True,
    ):
        cfg = SweeperConfig.from_env()
    assert cfg.interval_seconds == DEFAULT_INTERVAL_SECONDS


def test_config_falls_back_to_default_for_negative_int():
    """Zero or negative intervals would cause a tight loop; we treat
    them as misconfiguration and use the safe default."""
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": "postgresql://x:x@127.0.0.1:5432/x",
            "STRATHON_WEBHOOK_SWEEPER_INTERVAL_SEC": "-5",
        },
        clear=True,
    ):
        cfg = SweeperConfig.from_env()
    assert cfg.interval_seconds == DEFAULT_INTERVAL_SECONDS


# ---- sweep_once --------------------------------------------------------


async def _make_orphan(session, project_id, *, age_seconds=600):
    """Insert one pending delivery with a stale next_attempt_at."""
    policy_id = uuid.uuid4()
    await session.execute(insert(Policy).values(
        id=policy_id, project_id=project_id,
        name=f"sweeper_test_{policy_id.hex[:6]}",
        description="", match_expression="true",
        action="alert",
        action_config={"webhook_url": "https://example.test/h"},
        applies_to=[], enabled=True, priority=0,
    ))
    delivery_id = uuid.uuid4()
    await session.execute(insert(WebhookDelivery).values(
        id=delivery_id,
        project_id=project_id, policy_id=policy_id,
        webhook_id=f"msg_{delivery_id.hex[:12]}",
        url="https://example.test/h",
        payload={"x": 1},
        status="pending",
        attempts=0,
        next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    ))
    await session.flush()
    return delivery_id


async def test_sweep_once_returns_zero_when_no_orphans(
    async_engine, isolated_project,
):
    """A sweep with a threshold so large nothing qualifies returns 0.

    We pick a 1-year threshold to be insensitive to whatever orphan
    rows other tests have committed. The point of this test is to
    prove sweep_once doesn't crash on empty input — not to assert
    on global DB state.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    dispatched = await sweep_once(
        maker, threshold_seconds=365 * 24 * 3600, batch_limit=10,
    )
    assert dispatched == 0


async def test_sweep_once_dispatches_orphans(async_engine):
    """A row that's been pending too long gets re-dispatched.
    Uses a committed project + policy + delivery so the new session
    sees them."""
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from models import Project, ProjectSettings

    project_id = uuid.uuid4()
    slug = f"sweep-{project_id.hex[:8]}"

    async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
        await s.execute(insert(Project).values(id=project_id, name=slug, slug=slug))
        await s.execute(insert(ProjectSettings).values(project_id=project_id))
        await _make_orphan(s, project_id)
        await s.commit()

    try:
        maker = async_sessionmaker(async_engine, expire_on_commit=False)
        with patch("webhooks.actor.deliver_webhook") as send_mock:
            send_mock.send = lambda *a, **kw: None
            dispatched = await sweep_once(
                maker, threshold_seconds=60, batch_limit=10,
            )
        # The sweeper query is global (it picks up orphans across all
        # projects), so other tests may contribute additional rows. Assert
        # we dispatched AT LEAST the one we inserted.
        assert dispatched >= 1
    finally:
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            await s.execute(
                delete(WebhookDelivery).where(WebhookDelivery.project_id == project_id)
            )
            await s.execute(delete(Policy).where(Policy.project_id == project_id))
            await s.execute(
                delete(ProjectSettings).where(ProjectSettings.project_id == project_id)
            )
            await s.execute(delete(Project).where(Project.id == project_id))
            await s.commit()


# ---- sweeper_loop lifecycle --------------------------------------------


async def test_loop_returns_immediately_when_disabled():
    """An operator disabling the sweeper via env should see the task
    exit immediately, not block forever in the sleep."""
    cfg = SweeperConfig(
        enabled=False, interval_seconds=1,
        threshold_seconds=60, batch_limit=10,
    )
    shutdown = asyncio.Event()
    # Should exit promptly without ever calling sweep_once
    await asyncio.wait_for(
        sweeper_loop(cfg, shutdown, session_maker=None),
        timeout=2.0,
    )


async def test_loop_exits_when_shutdown_event_set():
    """A graceful shutdown must wake the sleep, not wait the full
    interval. We pick interval_seconds=60 to prove we don't wait that
    long — if the test took 60s+ to complete, the sleep wasn't
    interruptible."""
    cfg = SweeperConfig(
        enabled=True, interval_seconds=60,
        threshold_seconds=60, batch_limit=10,
    )
    shutdown = asyncio.Event()

    # Set shutdown after a tiny delay so the loop has time to start
    async def _trigger():
        await asyncio.sleep(0.1)
        shutdown.set()

    # Use a dummy session maker that the sweep_once call won't actually
    # hit (because the orphan-finder returns empty quickly).
    fake_maker = lambda: type("M", (), {  # noqa: E731
        "__aenter__": lambda self: self,
        "__aexit__": lambda *a: None,
    })()

    # If the sleep ISN'T interruptible this hangs forever. Time-box.
    with patch("webhooks.sweeper.sweep_once", return_value=0):
        await asyncio.wait_for(
            asyncio.gather(
                sweeper_loop(cfg, shutdown, session_maker=fake_maker),
                _trigger(),
            ),
            timeout=5.0,
        )


async def test_loop_continues_after_tick_exception():
    """A failed sweep tick must not kill the loop — it logs and waits
    for the next interval."""
    cfg = SweeperConfig(
        enabled=True, interval_seconds=0.05,  # tight interval for test speed
        threshold_seconds=60, batch_limit=10,
    )
    shutdown = asyncio.Event()

    call_count = {"n": 0}

    async def _flaky_sweep(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated DB error")
        return 0

    fake_maker = lambda: None  # noqa: E731 - sweep_once is mocked, maker unused

    async def _stop_after_two_ticks():
        # Wait until sweep_once has been called at least twice
        while call_count["n"] < 2:
            await asyncio.sleep(0.02)
        shutdown.set()

    with patch("webhooks.sweeper.sweep_once", side_effect=_flaky_sweep):
        await asyncio.wait_for(
            asyncio.gather(
                sweeper_loop(cfg, shutdown, session_maker=fake_maker),
                _stop_after_two_ticks(),
            ),
            timeout=5.0,
        )

    # Both ticks ran — first raised, second succeeded
    assert call_count["n"] >= 2

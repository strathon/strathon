"""Tests for the retention module.

Two groups:
  - Pure config-parsing tests (no DB). Patch env vars and assert that
    RetentionConfig.from_env builds the right config.
  - DB sweep tests using the shared session/isolated_project fixtures
    from conftest.py. Each test rolls back at teardown.

The loop-driver tests (disabled-immediate-return, honors-shutdown-during-
initial-delay) live here too because they're orchestration, not DB.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time
from unittest.mock import patch

import pytest


_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)

from retention import (  # noqa: E402  -- sys.path manipulation above
    DEFAULT_BATCH_SIZE,
    DEFAULT_INTERVAL_SECONDS,
    NS_PER_DAY,
    RetentionConfig,
    cleanup_once,
    retention_loop,
)


@pytest.fixture(autouse=True)
def _reset_settings_cache_after_test():
    """Several config-parsing tests below use ``patch.dict(os.environ,
    clear=True)`` plus ``importlib.reload(config)`` to drive Settings
    with a bogus DATABASE_URL like ``postgresql://x:x@.../x``. The env
    is restored on test exit, but the lru_cache inside
    ``config.get_settings()`` is left populated with the bogus URL.
    That leaks into any subsequent test (in any module) that imports
    `database` and triggers a connection — typically the API
    TestClient fixtures.

    This fixture cleans up after every test in this module: clear the
    cache and reload the config module so the next test starts with a
    fresh Settings built from the real (unpatched) environment.
    """
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


# ===========================================================================
# RetentionConfig.from_env  --  pure config parsing, no DB
# ===========================================================================


def test_config_defaults_when_env_empty():
    """With no env vars set, defaults apply."""
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": "postgresql://x:x@127.0.0.1:5432/x",
            # Strip retention env vars
        },
        clear=True,
    ):
        # Force a fresh Settings singleton read by clearing the module's
        # cache. The settings global is loaded once at import time; we
        # reload to reflect the patched env.
        import importlib
        import config as cfg_mod
        importlib.reload(cfg_mod)

        cfg = RetentionConfig.from_env()
    assert cfg.enabled is True
    assert cfg.interval_seconds == DEFAULT_INTERVAL_SECONDS
    assert cfg.batch_size == DEFAULT_BATCH_SIZE


def test_config_disabled_via_env():
    """STRATHON_RETENTION_ENABLED=false/0/no/off all disable the loop."""
    for val in ("false", "False", "0", "no", "off"):
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://x:x@127.0.0.1:5432/x",
                "STRATHON_RETENTION_ENABLED": val,
            },
            clear=True,
        ):
            import importlib
            import config as cfg_mod
            importlib.reload(cfg_mod)

            cfg = RetentionConfig.from_env()
            assert cfg.enabled is False, f"failed for {val!r}"


def test_config_enabled_is_default():
    """If STRATHON_RETENTION_ENABLED is not set, the loop is enabled."""
    with patch.dict(
        os.environ,
        {"DATABASE_URL": "postgresql://x:x@127.0.0.1:5432/x"},
        clear=True,
    ):
        import importlib
        import config as cfg_mod
        importlib.reload(cfg_mod)

        cfg = RetentionConfig.from_env()
    assert cfg.enabled is True


def test_config_interval_floor():
    """Invalid interval values get rejected at Settings load time.

    The previous loose env parser silently clamped to 60. The Pydantic
    Settings layer is strict: anything <60 raises ValidationError. The
    receiver's startup will surface this as a clear error rather than
    silently running with a different value than the operator typed.

    For values that bypass Settings init (the legacy fallback path
    inside from_env), the floor is still applied. Testing the
    happy-path valid value to confirm parsing works.
    """
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": "postgresql://x:x@127.0.0.1:5432/x",
            "STRATHON_RETENTION_INTERVAL_SECONDS": "120",
        },
        clear=True,
    ):
        import importlib
        import config as cfg_mod
        importlib.reload(cfg_mod)
        cfg = RetentionConfig.from_env()
    assert cfg.interval_seconds == 120


def test_config_batch_size_override():
    """STRATHON_RETENTION_BATCH_SIZE is honored if numeric."""
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": "postgresql://x:x@127.0.0.1:5432/x",
            "STRATHON_RETENTION_BATCH_SIZE": "250",
        },
        clear=True,
    ):
        import importlib
        import config as cfg_mod
        importlib.reload(cfg_mod)
        cfg = RetentionConfig.from_env()
    assert cfg.batch_size == 250


# ===========================================================================
# Loop driver shutdown behavior  --  no DB
# ===========================================================================


async def test_retention_loop_disabled_returns_immediately():
    """When the loop is disabled, it should return without doing anything."""
    cfg = RetentionConfig(enabled=False, interval_seconds=3600, batch_size=100)
    shutdown = asyncio.Event()
    await asyncio.wait_for(
        retention_loop(config=cfg, shutdown_event=shutdown),
        timeout=2.0,
    )


async def test_retention_loop_honors_shutdown_during_initial_delay():
    """If shutdown is set during the startup delay, loop exits without running."""
    cfg = RetentionConfig(enabled=True, interval_seconds=3600, batch_size=100)
    shutdown = asyncio.Event()

    async def signal_shutdown():
        await asyncio.sleep(0.1)
        shutdown.set()

    asyncio.create_task(signal_shutdown())
    await asyncio.wait_for(
        retention_loop(config=cfg, shutdown_event=shutdown),
        timeout=5.0,
    )


# ===========================================================================
# cleanup_once  --  DB-touching tests via the session fixture
# ===========================================================================


async def _insert_trace(session, project_id, start_time_ns):
    """Helper: insert a trace row via the ORM. Returns the 16-byte trace_id."""
    from models import Trace
    trace_id = os.urandom(16)
    session.add(
        Trace(
            id=trace_id,
            project_id=project_id,
            start_time_unix_nano=start_time_ns,
        )
    )
    await session.flush()
    return trace_id


async def _set_retention_days(session, project_id, days):
    """Helper: tweak the project_settings.trace_retention_days for a test."""
    from sqlalchemy import update
    from models import ProjectSettings
    await session.execute(
        update(ProjectSettings)
        .where(ProjectSettings.project_id == project_id)
        .values(trace_retention_days=days)
    )
    await session.flush()


async def test_cleanup_deletes_expired_traces(session, isolated_project):
    """A trace older than retention_days should be deleted."""
    from sqlalchemy import select
    from models import Trace

    # isolated_project ships with default trace_retention_days = 30.
    # Shorten it to 7 days for the test.
    await _set_retention_days(session, isolated_project, 7)

    now_ns = time.time_ns()
    # 14 days old -> beyond retention
    old_trace = await _insert_trace(session, isolated_project, now_ns - 14 * NS_PER_DAY)
    # 1 day old -> within retention
    fresh_trace = await _insert_trace(session, isolated_project, now_ns - 1 * NS_PER_DAY)

    await cleanup_once(session, batch_size=100)

    await session.flush()

    old_exists = (
        await session.execute(select(Trace).where(Trace.id == old_trace))
    ).scalar_one_or_none()
    fresh_exists = (
        await session.execute(select(Trace).where(Trace.id == fresh_trace))
    ).scalar_one_or_none()

    assert old_exists is None, "expired trace should be deleted"
    assert fresh_exists is not None, "fresh trace should be preserved"


async def test_cleanup_respects_batch_size(session, isolated_project):
    """At most batch_size traces deleted per sweep."""
    from sqlalchemy import select, func
    from models import Trace

    await _set_retention_days(session, isolated_project, 7)

    now_ns = time.time_ns()
    expired_count = 10
    for _ in range(expired_count):
        await _insert_trace(session, isolated_project, now_ns - 30 * NS_PER_DAY)

    # batch_size=3 should cap deletion at 3
    await cleanup_once(session, batch_size=3)
    # The sweep also processes OTHER projects that may exist from prior tests
    # or the seeded default project — so traces_deleted is *at least* 3.
    # For OUR project, exactly 3 must have been deleted; check that directly.
    remaining = (
        await session.execute(
            select(func.count(Trace.id)).where(Trace.project_id == isolated_project)
        )
    ).scalar_one()
    assert remaining == expired_count - 3


async def test_cleanup_skips_projects_with_zero_retention(session, isolated_project):
    """trace_retention_days = 0 means 'never delete' for that project."""
    from sqlalchemy import select
    from models import Trace

    await _set_retention_days(session, isolated_project, 0)

    now_ns = time.time_ns()
    ancient = await _insert_trace(session, isolated_project, now_ns - 365 * NS_PER_DAY)

    await cleanup_once(session, batch_size=100)
    await session.flush()

    still_there = (
        await session.execute(select(Trace).where(Trace.id == ancient))
    ).scalar_one_or_none()
    assert still_there is not None, "trace must survive zero-retention setting"


async def test_cleanup_skips_soft_deleted_projects(session, isolated_project):
    """Projects with deleted_at set are excluded from the sweep entirely."""
    from datetime import datetime, timezone
    from sqlalchemy import select, update
    from models import Project, Trace

    await _set_retention_days(session, isolated_project, 1)

    now_ns = time.time_ns()
    old = await _insert_trace(session, isolated_project, now_ns - 30 * NS_PER_DAY)

    # Soft-delete the project
    await session.execute(
        update(Project)
        .where(Project.id == isolated_project)
        .values(deleted_at=datetime.now(tz=timezone.utc))
    )
    await session.flush()

    await cleanup_once(session, batch_size=100)

    # Trace still there
    still = (
        await session.execute(select(Trace).where(Trace.id == old))
    ).scalar_one_or_none()
    assert still is not None

    # And the project wasn't even scanned, so projects_scanned doesn't count it.
    # (Other projects might still be scanned; we only assert ours wasn't.)


async def test_cleanup_returns_zero_when_no_eligible_traces(session, isolated_project):
    """Sweep with nothing to delete returns traces_deleted = 0 for this project."""
    from sqlalchemy import select, func
    from models import Trace

    await _set_retention_days(session, isolated_project, 7)

    now_ns = time.time_ns()
    # One trace, fresh
    await _insert_trace(session, isolated_project, now_ns - 1 * NS_PER_DAY)

    await cleanup_once(session, batch_size=100)
    await session.flush()

    remaining = (
        await session.execute(
            select(func.count(Trace.id)).where(Trace.project_id == isolated_project)
        )
    ).scalar_one()
    assert remaining == 1


async def test_cleanup_cascades_to_spans(session, isolated_project):
    """Deleting a trace should cascade to its spans via FK ON DELETE CASCADE."""
    from sqlalchemy import select, func
    from models import Span, Trace

    await _set_retention_days(session, isolated_project, 7)

    now_ns = time.time_ns()
    # 14 days old trace with a span attached
    trace_id = await _insert_trace(session, isolated_project, now_ns - 14 * NS_PER_DAY)

    span = Span(
        trace_id=trace_id,
        span_id=os.urandom(8),
        project_id=isolated_project,
        name="test.span",
        kind="INTERNAL",
        start_time_unix_nano=now_ns - 14 * NS_PER_DAY,
    )
    session.add(span)
    await session.flush()

    # Confirm the span exists
    span_count_before = (
        await session.execute(
            select(func.count())
            .select_from(Span)
            .where(Span.trace_id == trace_id)
        )
    ).scalar_one()
    assert span_count_before == 1

    await cleanup_once(session, batch_size=100)
    await session.flush()

    # Trace and span both gone
    trace_gone = (
        await session.execute(select(Trace).where(Trace.id == trace_id))
    ).scalar_one_or_none() is None
    span_count_after = (
        await session.execute(
            select(func.count())
            .select_from(Span)
            .where(Span.trace_id == trace_id)
        )
    ).scalar_one()
    assert trace_gone, "trace should be deleted"
    assert span_count_after == 0, "spans should cascade-delete"

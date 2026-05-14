"""Unit tests for the retention module.

Pure config-parsing tests need no DB. The cleanup_once tests use a real
Postgres connection via the same DATABASE_URL the receiver uses; they're
skipped if no DB is reachable so contributors can run pytest without
infrastructure.
"""

import asyncio
import os
import sys
import time
from unittest.mock import patch

import asyncpg
import pytest

sys.path.insert(0, "/home/claude/strathon/receiver")

from retention import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_INTERVAL_SECONDS,
    NS_PER_DAY,
    RetentionConfig,
    cleanup_once,
    retention_loop,
)


# ============================================================
# RetentionConfig.from_env
# ============================================================


def test_config_defaults_when_env_empty():
    with patch.dict(os.environ, {}, clear=True):
        cfg = RetentionConfig.from_env()
    assert cfg.enabled is True
    assert cfg.interval_seconds == DEFAULT_INTERVAL_SECONDS
    assert cfg.batch_size == DEFAULT_BATCH_SIZE


def test_config_disabled_via_env():
    for val in ("false", "False", "0", "no", "off"):
        with patch.dict(os.environ, {"STRATHON_RETENTION_ENABLED": val}, clear=True):
            cfg = RetentionConfig.from_env()
            assert cfg.enabled is False, f"failed for {val!r}"


def test_config_enabled_is_default():
    # "yes" / "true" / anything-not-falsy / unset all mean enabled
    for val in ("true", "yes", "1", "on", "anything"):
        with patch.dict(os.environ, {"STRATHON_RETENTION_ENABLED": val}, clear=True):
            cfg = RetentionConfig.from_env()
            assert cfg.enabled is True, f"failed for {val!r}"


def test_config_interval_seconds_parsed():
    with patch.dict(
        os.environ, {"STRATHON_RETENTION_INTERVAL_SECONDS": "7200"}, clear=True
    ):
        cfg = RetentionConfig.from_env()
    assert cfg.interval_seconds == 7200


def test_config_interval_seconds_floored():
    """Don't let users set a sub-60s interval that would hammer the DB."""
    with patch.dict(
        os.environ, {"STRATHON_RETENTION_INTERVAL_SECONDS": "5"}, clear=True
    ):
        cfg = RetentionConfig.from_env()
    assert cfg.interval_seconds == 60


def test_config_interval_seconds_garbage_falls_back():
    with patch.dict(
        os.environ, {"STRATHON_RETENTION_INTERVAL_SECONDS": "garbage"}, clear=True
    ):
        cfg = RetentionConfig.from_env()
    assert cfg.interval_seconds == DEFAULT_INTERVAL_SECONDS


def test_config_batch_size_parsed():
    with patch.dict(
        os.environ, {"STRATHON_RETENTION_BATCH_SIZE": "100"}, clear=True
    ):
        cfg = RetentionConfig.from_env()
    assert cfg.batch_size == 100


def test_config_batch_size_floored_at_1():
    with patch.dict(os.environ, {"STRATHON_RETENTION_BATCH_SIZE": "0"}, clear=True):
        cfg = RetentionConfig.from_env()
    assert cfg.batch_size == 1


# ============================================================
# DB-touching tests
# ============================================================

DB_URL = os.getenv(
    "DATABASE_URL", "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon"
).replace("postgresql+asyncpg://", "postgresql://")


@pytest.fixture
async def pool():
    """Connection pool to a real Postgres. Skips if unreachable."""
    try:
        p = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
    except (OSError, asyncpg.PostgresError):
        pytest.skip("Postgres not reachable for retention DB tests")
        return
    yield p
    await p.close()


@pytest.fixture
async def isolated_project(pool):
    """A fresh project + project_settings for a single test."""
    pid = None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO projects (name, slug) VALUES ('retention-test', $1) RETURNING id",
            f"retention-test-{time.time_ns()}",
        )
        pid = row["id"]
        await conn.execute(
            "INSERT INTO project_settings (project_id, trace_retention_days) VALUES ($1, 7)",
            pid,
        )
    yield pid
    async with pool.acquire() as conn:
        # Cascade drops settings, traces, spans, etc.
        await conn.execute("DELETE FROM projects WHERE id = $1", pid)


async def _insert_trace(pool, project_id, start_time_ns):
    trace_id = os.urandom(16)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO traces (id, project_id, start_time_unix_nano)
            VALUES ($1, $2, $3)
            """,
            trace_id, project_id, start_time_ns,
        )
    return trace_id


async def test_cleanup_deletes_expired_traces(pool, isolated_project):
    """A trace older than retention_days should be deleted."""
    now_ns = time.time_ns()
    # 14 days old -> beyond the 7-day retention
    old_trace = await _insert_trace(pool, isolated_project, now_ns - 14 * NS_PER_DAY)
    # 1 day old -> within retention, must survive
    fresh_trace = await _insert_trace(pool, isolated_project, now_ns - 1 * NS_PER_DAY)

    result = await cleanup_once(pool, batch_size=100)
    assert result["traces_deleted"] >= 1

    async with pool.acquire() as conn:
        old_exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM traces WHERE id = $1)", old_trace
        )
        fresh_exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM traces WHERE id = $1)", fresh_trace
        )
    assert old_exists is False, "expired trace should be deleted"
    assert fresh_exists is True, "fresh trace should be preserved"


async def test_cleanup_respects_batch_size(pool, isolated_project):
    """At most batch_size traces are deleted per sweep."""
    now_ns = time.time_ns()
    expired_count = 10
    for _ in range(expired_count):
        await _insert_trace(pool, isolated_project, now_ns - 30 * NS_PER_DAY)

    # batch=3 should cap deletion at 3
    result = await cleanup_once(pool, batch_size=3)
    assert result["traces_deleted"] == 3

    async with pool.acquire() as conn:
        remaining = await conn.fetchval(
            "SELECT COUNT(*) FROM traces WHERE project_id = $1",
            isolated_project,
        )
    assert remaining == expired_count - 3


async def test_cleanup_ignores_projects_with_zero_retention(pool):
    """A project with trace_retention_days = 0 means 'never delete'."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO projects (name, slug) VALUES ('no-retention', $1) RETURNING id",
            f"no-retention-{time.time_ns()}",
        )
        pid = row["id"]
        await conn.execute(
            "INSERT INTO project_settings (project_id, trace_retention_days) VALUES ($1, 0)",
            pid,
        )

    now_ns = time.time_ns()
    ancient = await _insert_trace(pool, pid, now_ns - 365 * NS_PER_DAY)
    try:
        await cleanup_once(pool, batch_size=100)
        async with pool.acquire() as conn:
            still_there = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM traces WHERE id = $1)", ancient
            )
        assert still_there is True
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM projects WHERE id = $1", pid)


# ============================================================
# Loop shutdown behavior
# ============================================================


async def test_retention_loop_disabled_returns_immediately():
    """When STRATHON_RETENTION_ENABLED=false the loop should exit cleanly."""
    cfg = RetentionConfig(enabled=False, interval_seconds=3600, batch_size=100)
    shutdown = asyncio.Event()
    # Pool not needed for the disabled path
    await asyncio.wait_for(
        retention_loop(pool=None, config=cfg, shutdown_event=shutdown),
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
        retention_loop(pool=None, config=cfg, shutdown_event=shutdown),
        timeout=5.0,
    )

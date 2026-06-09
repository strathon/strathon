"""Shared fixtures for receiver tests.

The session-based fixtures here are the standard pattern for testing
SQLAlchemy 2.0 async repositories. Each test gets its own session,
each test rolls back its work at the end so the DB stays clean between
tests, and tests skip cleanly when no Postgres is reachable so a
contributor can run `pytest` on an offline laptop without infrastructure.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

# Receiver is not packaged; add to sys.path so tests can import its modules.
# Resolve relative to this file so the same conftest works on any machine.
_HERE = os.path.dirname(os.path.abspath(__file__))
_RECEIVER_DIR = os.path.dirname(_HERE)
if _RECEIVER_DIR not in sys.path:
    sys.path.insert(0, _RECEIVER_DIR)


DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://strathon:strathon_dev@localhost:5432/strathon",
)


# ---- Audit log HMAC key ----------------------------------------------------
#
# Every mutation endpoint calls ``audit.emit`` which loads the configured
# HMAC key from ``STRATHON_AUDIT_HMAC_KEY``. The receiver refuses to start
# in production with no key, and tests aren't production — but CI's test
# environment doesn't set the env var either, so without a default the
# entire TestClient-based test suite (api_key_scopes, audit_api, ...)
# fails at app lifespan with ``RuntimeError: STRATHON_AUDIT_HMAC_KEY is
# required in production``.
#
# We could solve this with autouse fixtures in every test file that uses
# TestClient, but the cleaner solution is a single deterministic default
# at conftest module load. Tests that want to exercise the fail-closed
# path (e.g. ``test_emit_fails_closed_with_empty_key_in_prod``) explicitly
# unset / override via ``monkeypatch.setenv`` and clear the lru_cache, so
# they continue to work.
#
# The value is fixed (not randomized) so test runs are reproducible.
# 64 hex chars = 32 bytes, the minimum the receiver accepts.
os.environ.setdefault(
    "STRATHON_AUDIT_HMAC_KEY",
    "test_audit_hmac_key_do_not_use_in_production_aaaaaaaaaaaaaaaaaaaa",
)

# Disable webhook SSRF guard in tests. Tests use mock transports with
# non-routable hostnames (example.test) that would fail DNS resolution.
os.environ.setdefault("STRATHON_WEBHOOK_SSRF_GUARD", "false")

# Enable interactive docs in tests (disabled by default in production).
os.environ.setdefault("STRATHON_DOCS_ENABLED", "true")


@pytest_asyncio.fixture
async def async_engine():
    """Async SQLAlchemy engine pointing at the test DB.

    Skips the test if Postgres isn't reachable. Engine is disposed at
    teardown so connections don't leak between tests.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    # Normalize URL through config so the same rewriter logic applies as
    # the receiver runtime uses.
    from config import Settings
    settings = Settings(DATABASE_URL=DB_URL)

    engine = create_async_engine(settings.async_database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("Postgres not reachable for repository tests")

    # Partition coverage for the test DB.
    #
    # The spans tables are RANGE-partitioned on start_time_unix_nano. Tests
    # insert at two kinds of timestamps: tiny synthetic values (1000, 2000,
    # 1_000_000_000_000, the 1.7e18 fixtures) and real wall-clock values
    # (time.time()-based, "now"). Every inserted row must land in some
    # partition or Postgres raises "no partition of relation found".
    #
    # We cover this deterministically rather than relying on a hardcoded bound:
    #   1. ensure_partitions() creates the production months (previous month
    #      through +PREMAKE_MONTHS, relative to now) using the real worker code,
    #      so "now"-dated inserts always land and the real partition routing is
    #      exercised. This tracks wall-clock automatically.
    #   2. A historical catch-all covers [0, <start of the earliest production
    #      month>) so every synthetic value below the real partitions lands too.
    #      Bounding it at the earliest ensured month (not a hardcoded date)
    #      leaves NO gap between the synthetic range and the real partitions.
    from datetime import datetime, timezone

    from spans_worker import (
        _advance_month,
        _month_bounds_ns,
        ensure_partitions,
    )

    async with engine.begin() as conn:
        # Disable audit immutability triggers in the test DB so test cleanup
        # (DELETE FROM audit.events) works. Production triggers prevent
        # UPDATE/DELETE on audit tables.
        await conn.execute(text(
            "ALTER TABLE audit.events DISABLE TRIGGER trg_events_immutable"
        ))
        await conn.execute(text(
            "ALTER TABLE audit.anchors DISABLE TRIGGER trg_anchors_immutable"
        ))

    # Create the real production months via the worker (commits internally).
    async with AsyncSession(engine) as session:
        await ensure_partitions(session)

    # Historical catch-all up to the earliest production month, so synthetic
    # low timestamps and any pre-"now" fixtures land without a gap.
    _now = datetime.now(timezone.utc)
    _earliest_year, _earliest_month = _advance_month(_now.year, _now.month, -1)
    _earliest_start_ns, _ = _month_bounds_ns(_earliest_year, _earliest_month)
    async with engine.begin() as conn:
        for tbl in ("spans", "span_events", "span_links"):
            await conn.execute(text(
                f"CREATE TABLE IF NOT EXISTS {tbl}_test "
                f"PARTITION OF {tbl} "
                f"FOR VALUES FROM (0) TO ({_earliest_start_ns})"
            ))

    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session(async_engine) -> AsyncGenerator:
    """Per-test AsyncSession. Auto-rollback at teardown.

    The session is wrapped in a transaction that's rolled back when the
    test ends, so anything the test inserts vanishes. This is the
    standard pattern for keeping the DB clean between tests without
    truncating tables or recreating the schema.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    async with async_engine.connect() as conn:
        trans = await conn.begin()
        try:
            async_session = AsyncSession(bind=conn, expire_on_commit=False)
            try:
                yield async_session
            finally:
                await async_session.close()
        finally:
            await trans.rollback()


@pytest_asyncio.fixture
async def isolated_project(session) -> AsyncGenerator:
    """A fresh project for the test. Rolled back at teardown.

    Use this when a test needs a project_id to attach data to. The
    project never persists past the test because the session rolls back.
    """
    from sqlalchemy import insert

    from models import Project, ProjectSettings

    project_id = uuid.uuid4()
    slug = f"test-{project_id.hex[:8]}"

    # Insert project + its settings row. The seeded migration relies on
    # every project having a settings row, so we mirror that here. Every
    # project belongs to an organization; tests use the default org.
    default_org_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
    await session.execute(
        insert(Project).values(
            id=project_id, name=f"Test {slug}", slug=slug, org_id=default_org_id
        )
    )
    await session.execute(
        insert(ProjectSettings).values(project_id=project_id)
    )
    await session.flush()

    yield project_id

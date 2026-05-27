"""Tests for the /ready readiness probe and its sub-checks.

Covers:
    - /health regression (unchanged behavior)
    - /ready happy path (all dependencies healthy)
    - /ready failure paths for each sub-check
    - Response shape stability across success and failure
    - Unit tests for each check helper in isolation
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)


DEFAULT_DB_URL = "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon"


# ---- Unit tests: _script_head_revision -----------------------------------


def test_script_head_revision_returns_non_empty_string():
    """The bundled migrations should always have a head revision."""
    from api.health import _script_head_revision

    head = _script_head_revision()
    assert isinstance(head, str)
    assert len(head) > 0


def test_script_head_revision_matches_latest_migration_file():
    """Head revision should correspond to one of the files in alembic/versions/."""
    from api.health import _script_head_revision

    head = _script_head_revision()
    versions_dir = os.path.join(_RECEIVER_DIR, "alembic", "versions")
    version_files = [f for f in os.listdir(versions_dir) if f.endswith(".py")]
    # head should appear as a prefix in at least one filename (revision IDs are
    # the leading digits of the version-file basenames in this repo).
    assert any(head in f for f in version_files), (
        f"head {head!r} not found in any of {version_files}"
    )


# ---- Unit tests: _check_background_task ----------------------------------


class _FakeState:
    """Minimal object with attribute access, used in lieu of FastAPI's State."""


def test_check_background_task_missing_attr_fails():
    from api.health import _check_background_task

    result = _check_background_task(_FakeState(), "nonexistent_task")
    assert result["status"] == "failed"
    assert "not registered" in result["reason"]


@pytest.mark.asyncio
async def test_check_background_task_running_is_ok():
    from api.health import _check_background_task

    async def _runs_forever():
        await asyncio.sleep(60)

    task = asyncio.create_task(_runs_forever())
    state = _FakeState()
    state.my_task = task
    try:
        result = _check_background_task(state, "my_task")
        assert result == {"status": "ok"}
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_check_background_task_crashed_with_exception_fails():
    from api.health import _check_background_task

    async def _crashes():
        raise RuntimeError("simulated crash")

    task = asyncio.create_task(_crashes())
    # Let it run and crash.
    try:
        await task
    except RuntimeError:
        pass

    state = _FakeState()
    state.my_task = task
    result = _check_background_task(state, "my_task")
    assert result["status"] == "failed"
    assert "RuntimeError" in result["reason"]
    assert "simulated crash" in result["reason"]


@pytest.mark.asyncio
async def test_check_background_task_cancelled_fails():
    from api.health import _check_background_task

    async def _runs_forever():
        await asyncio.sleep(60)

    task = asyncio.create_task(_runs_forever())
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    state = _FakeState()
    state.my_task = task
    result = _check_background_task(state, "my_task")
    assert result["status"] == "failed"
    assert "cancel" in result["reason"].lower()


@pytest.mark.asyncio
async def test_check_background_task_returned_normally_fails():
    """Background loops aren't supposed to return; a clean exit is degraded."""
    from api.health import _check_background_task

    async def _exits_normally():
        return None

    task = asyncio.create_task(_exits_normally())
    await task

    state = _FakeState()
    state.my_task = task
    result = _check_background_task(state, "my_task")
    assert result["status"] == "failed"
    assert "exited" in result["reason"].lower()


# ---- Unit tests: _check_db -----------------------------------------------


@pytest.mark.asyncio
async def test_check_db_failure_when_db_unreachable():
    """With a bogus DATABASE_URL the engine fails to connect, _check_db reports
    failed with a useful reason. Cleans up cached engine so other tests aren't
    affected."""
    from api.health import _check_db
    from database import get_engine, get_session_maker

    original_db_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = "postgresql://strathon:wrong@127.0.0.1:5/nothing"

    # Reset cached settings + engine so the bad URL is picked up.
    from config import get_settings
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_maker.cache_clear()

    try:
        result = await _check_db()
        assert result["status"] == "failed"
        assert "reason" in result
    finally:
        # Restore env + caches.
        if original_db_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_db_url
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_maker.cache_clear()


# ---- Integration tests: /health and /ready via TestClient ----------------


@pytest.fixture(scope="module")
def client():
    db_url = os.getenv("DATABASE_URL", DEFAULT_DB_URL)
    os.environ["DATABASE_URL"] = db_url
    try:
        import psycopg
        conn = psycopg.connect(db_url, autocommit=True)
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable")

    # Reset any cached engine the unit tests above may have left around with a
    # different URL, so the TestClient builds a fresh one against the real DB.
    from config import get_settings
    from database import get_engine, get_session_maker
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_maker.cache_clear()

    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        yield c


def test_health_endpoint_unchanged(client):
    """Liveness probe should still return the simple status payload."""
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "strathon-receiver"
    assert "version" in body


def test_ready_returns_200_when_all_healthy(client):
    r = client.get("/ready")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ready"
    assert set(body["checks"].keys()) == {
        "db",
        "migrations",
        "partitions",
        "retention_task",
        "webhook_sweeper_task",
        "budget_monitor_task",
        "audit_partition_task",
        "spans_partition_task",
    }
    for name, check in body["checks"].items():
        assert check["status"] == "ok", f"{name} failed: {check}"


def test_ready_db_check_includes_latency_on_success(client):
    r = client.get("/ready")
    body = r.json()
    db = body["checks"]["db"]
    assert db["status"] == "ok"
    assert "latency_ms" in db
    assert isinstance(db["latency_ms"], (int, float))
    assert db["latency_ms"] >= 0


def test_ready_migrations_check_reports_versions_on_success(client):
    r = client.get("/ready")
    body = r.json()
    mig = body["checks"]["migrations"]
    assert mig["status"] == "ok"
    assert mig["current"] == mig["head"]


def test_ready_returns_503_when_background_task_is_dead(client):
    """Replace a background task with a crashed one; /ready must flip to 503
    with the response shape preserved and the failing check identified."""
    import main

    original = main.app.state.budget_monitor_task

    async def _crashes():
        raise RuntimeError("budget monitor died for the test")

    crashed = asyncio.new_event_loop().run_until_complete(_capture_crashed_task(_crashes))
    main.app.state.budget_monitor_task = crashed
    try:
        r = client.get("/ready")
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "not_ready"
        # Shape is stable: all checks still present.
        assert set(body["checks"].keys()) == {
            "db",
            "migrations",
            "partitions",
            "retention_task",
            "webhook_sweeper_task",
            "budget_monitor_task",
            "audit_partition_task",
            "spans_partition_task",
        }
        # The dead one is flagged; the others are still ok.
        assert body["checks"]["budget_monitor_task"]["status"] == "failed"
        assert "RuntimeError" in body["checks"]["budget_monitor_task"]["reason"]
        assert body["checks"]["db"]["status"] == "ok"
        assert body["checks"]["migrations"]["status"] == "ok"
        assert body["checks"]["retention_task"]["status"] == "ok"
        assert body["checks"]["webhook_sweeper_task"]["status"] == "ok"
    finally:
        main.app.state.budget_monitor_task = original


def test_ready_returns_503_when_multiple_tasks_dead(client):
    """Two tasks dead at once; /ready reports both as failed."""
    import main

    original_monitor = main.app.state.budget_monitor_task
    original_sweeper = main.app.state.webhook_sweeper_task

    async def _crashes():
        raise ValueError("simulated failure")

    crashed_monitor = asyncio.new_event_loop().run_until_complete(_capture_crashed_task(_crashes))
    crashed_sweeper = asyncio.new_event_loop().run_until_complete(_capture_crashed_task(_crashes))
    main.app.state.budget_monitor_task = crashed_monitor
    main.app.state.webhook_sweeper_task = crashed_sweeper
    try:
        r = client.get("/ready")
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["budget_monitor_task"]["status"] == "failed"
        assert body["checks"]["webhook_sweeper_task"]["status"] == "failed"
    finally:
        main.app.state.budget_monitor_task = original_monitor
        main.app.state.webhook_sweeper_task = original_sweeper


def test_ready_endpoint_is_unauthenticated(client):
    """No Authorization header; /ready must still answer (it's a probe)."""
    r = client.get("/ready")
    # Either 200 (healthy) or 503 (unhealthy) — never 401/403.
    assert r.status_code in (200, 503)


# ---- Helpers --------------------------------------------------------------


async def _capture_crashed_task(coro_factory):
    """Run a coroutine that's expected to raise; return its (done) task.

    Used by the integration tests to inject a known-crashed task into
    app.state so the /ready endpoint sees task.done()=True with an
    exception captured.
    """
    task = asyncio.create_task(coro_factory())
    try:
        await task
    except BaseException:
        # Expected. The task object is what we want; the exception lives
        # on it and _check_background_task will surface it.
        pass
    return task

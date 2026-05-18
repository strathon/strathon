"""Health, readiness, and metrics endpoints.

All three are unauthenticated by design: Prometheus scrapers and Kubernetes
probes commonly run without credentials. Operators who want to restrict
them should do so at the network layer (ACL, reverse proxy).

Three endpoints, three concerns:

    /health    Liveness probe. "Is the process alive and able to handle a
               request at all?" Returns 200 unconditionally as long as the
               event loop is responsive. Anything heavier here defeats the
               point of a liveness probe (a stuck dependency would cause
               Kubernetes to kill an otherwise-healthy pod).

    /ready     Readiness probe. "Should traffic be routed to this replica
               right now?" Performs deep dependency checks: database
               connectivity, schema migration version, and the three
               background tasks (retention, webhook sweeper, budget
               monitor) that the receiver relies on. Returns 200 when all
               checks pass, 503 with a per-check breakdown when any
               dependency is unhealthy. Kubernetes will stop sending
               traffic to a replica whose readiness probe fails, then
               resume routing once it recovers.

    /metrics   Prometheus exposition.

The split between /health and /ready follows the Kubernetes convention.
A deeper readiness check is exactly what /ready is for: liveness must
stay lightweight so that a dependency hiccup doesn't trigger pod restarts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

import metrics as metrics_mod


logger = logging.getLogger(__name__)


router = APIRouter(tags=["health"])


# Each readiness sub-check is bounded so /ready stays well under the
# typical 1s Kubernetes probe timeout even when a dependency is degraded.
# Sub-second checks plus a 5s probe period mean a single slow check
# won't cascade into a probe failure.
_DB_CHECK_TIMEOUT_S = 0.5
_MIGRATION_CHECK_TIMEOUT_S = 0.5


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe.

    Lightweight by design: does not touch the database or any background
    task. The contract is "the process is alive and the event loop is
    responsive." For "is the service ready to serve traffic," use
    ``/ready`` instead.
    """
    return {"status": "ok", "service": "strathon-receiver", "version": "0.0.1"}


@router.get("/ready")
async def ready(request: Request) -> Response:
    """Readiness probe with deep dependency checks.

    Returns 200 with ``{"status": "ready", "checks": {...}}`` when every
    dependency is healthy. Returns 503 with the same shape (and
    ``"status": "not_ready"``) when any check fails. Each check has its
    own ``status`` field (``"ok"`` or ``"failed"``) plus check-specific
    detail fields. Failed checks include a ``reason`` field with a
    short human-readable summary.

    The response shape is stable: callers can rely on ``checks[name]``
    being present for every known check regardless of overall status, so
    a dashboard can render a per-check status row without conditional
    keys.
    """
    state = request.app.state

    db_check = await _check_db()
    migration_check = await _check_migrations()
    partition_check = await _check_partitions()
    retention_check = _check_background_task(state, "retention_task")
    sweeper_check = _check_background_task(state, "webhook_sweeper_task")
    monitor_check = _check_background_task(state, "budget_monitor_task")
    audit_part_check = _check_background_task(state, "audit_partition_task")
    spans_part_check = _check_background_task(state, "spans_partition_task")

    checks = {
        "db": db_check,
        "migrations": migration_check,
        "partitions": partition_check,
        "retention_task": retention_check,
        "webhook_sweeper_task": sweeper_check,
        "budget_monitor_task": monitor_check,
        "audit_partition_task": audit_part_check,
        "spans_partition_task": spans_part_check,
    }

    all_ok = all(c["status"] == "ok" for c in checks.values())
    body = {
        "status": "ready" if all_ok else "not_ready",
        "checks": checks,
    }
    status_code = 200 if all_ok else 503
    return JSONResponse(content=body, status_code=status_code)


@router.get("/metrics")
async def metrics_endpoint(request: Request) -> Response:
    """Prometheus exposition endpoint."""
    state = request.app.state
    # Mirror the latest SamplingCounters snapshot into the Prom counters
    snapshot = state.sampling_counters.snapshot()
    metrics_mod.sync_sampling_counters(state.metrics, snapshot)
    # Keep the sampling_rate gauge accurate in case it could ever change
    state.metrics.sampling_rate.set(state.sampling_config.sample_rate)

    body, content_type = metrics_mod.render_metrics(state.metrics)
    return Response(content=body, media_type=content_type)


# ---- Readiness check implementations --------------------------------------
#
# Each check returns a dict with at least {"status": "ok" | "failed"}. On
# failure the dict additionally has a "reason" field with a short
# human-readable summary. Success paths add check-specific detail
# (latency, version numbers, etc.) that's useful for operator dashboards
# but irrelevant to the pass/fail decision.


async def _check_db() -> dict[str, Any]:
    """Database connectivity check.

    Runs ``SELECT 1`` through the shared async engine. Bounded by
    ``_DB_CHECK_TIMEOUT_S`` so a hung Postgres doesn't stall the probe.
    """
    from database import get_session_maker

    start = time.perf_counter()
    try:
        async def _probe() -> None:
            session_maker = get_session_maker()
            async with session_maker() as session:
                await session.execute(text("SELECT 1"))

        await asyncio.wait_for(_probe(), timeout=_DB_CHECK_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {
            "status": "failed",
            "reason": f"db check exceeded {_DB_CHECK_TIMEOUT_S * 1000:.0f}ms timeout",
        }
    except Exception as exc:
        return {
            "status": "failed",
            "reason": f"db check raised {type(exc).__name__}: {exc}",
        }

    latency_ms = (time.perf_counter() - start) * 1000.0
    return {"status": "ok", "latency_ms": round(latency_ms, 2)}


async def _check_migrations() -> dict[str, Any]:
    """Schema migration version check.

    Compares the ``alembic_version`` row against the head revision
    declared in the bundled Alembic script directory. A receiver whose
    schema is behind the code (image rolled forward, migrations didn't
    run) is not ready to serve traffic — writes against the old schema
    will fail or, worse, silently drop new fields.

    Reads the script-directory head from local files (cheap, sync) and
    the ``alembic_version`` row via the async engine (bounded by
    ``_MIGRATION_CHECK_TIMEOUT_S``).
    """
    # Local-file read of the script directory's head. This is filesystem
    # I/O against the bundled migrations and doesn't need the DB.
    try:
        script_head = _script_head_revision()
    except Exception as exc:
        return {
            "status": "failed",
            "reason": f"could not read alembic script head: {type(exc).__name__}: {exc}",
        }

    # DB read of the currently-applied revision.
    from database import get_session_maker

    try:
        async def _probe() -> str | None:
            session_maker = get_session_maker()
            async with session_maker() as session:
                row = await session.execute(text("SELECT version_num FROM alembic_version"))
                return row.scalar_one_or_none()

        current = await asyncio.wait_for(_probe(), timeout=_MIGRATION_CHECK_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {
            "status": "failed",
            "reason": f"migrations check exceeded {_MIGRATION_CHECK_TIMEOUT_S * 1000:.0f}ms timeout",
        }
    except Exception as exc:
        return {
            "status": "failed",
            "reason": f"migrations check raised {type(exc).__name__}: {exc}",
        }

    if current is None:
        return {
            "status": "failed",
            "reason": "alembic_version table empty (database not migrated)",
            "head": script_head,
        }

    if current != script_head:
        return {
            "status": "failed",
            "reason": f"schema at {current}, code expects {script_head}",
            "current": current,
            "head": script_head,
        }

    return {"status": "ok", "current": current, "head": script_head}


def _script_head_revision() -> str:
    """Read the head revision from the bundled Alembic script directory.

    alembic.ini sits next to main.py in receiver/. Resolved relative to
    this file's location so the lookup works regardless of cwd, matching
    the pattern used by the auto-migrate path in main.py.
    """
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    # api/health.py -> receiver/api/health.py; alembic.ini -> receiver/alembic.ini
    receiver_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ini_path = os.path.join(receiver_dir, "alembic.ini")

    cfg = AlembicConfig(ini_path)
    # script_location in alembic.ini is relative to the .ini file by default,
    # but in some packaging layouts it can be cwd-relative; force the
    # config's main option so ScriptDirectory resolves the script tree
    # next to alembic.ini regardless of how the receiver was started.
    cfg.set_main_option("script_location", os.path.join(receiver_dir, "alembic"))

    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    if head is None:
        raise RuntimeError("alembic script directory has no head revision")
    return head


async def _check_partitions() -> dict[str, Any]:
    """Verify span partitions exist for the current month.

    If no partition covers the current month, span inserts will fail
    with a 'no partition' error. This catches cases where the
    partition maintenance worker died or hasn't run yet.

    Research: Kubernetes readiness probes should verify all critical
    dependencies (BetterStack 2025, CICube 2025). Missing partitions
    are a silent failure — the receiver appears healthy but inserts
    fail hard.
    """
    from datetime import datetime, timezone

    from sqlalchemy import text

    from database import async_session_maker

    now = datetime.now(timezone.utc)
    suffix = f"y{now.year}m{now.month:02d}"

    try:
        async with async_session_maker() as session:
            result = await asyncio.wait_for(
                session.execute(text(
                    "SELECT count(*) FROM pg_inherits "
                    "JOIN pg_class child ON child.oid = pg_inherits.inhrelid "
                    "JOIN pg_class parent ON parent.oid = pg_inherits.inhparent "
                    "WHERE parent.relname = 'spans' "
                    "AND child.relname = :expected"
                ), {"expected": f"spans_{suffix}"}),
                timeout=_DB_CHECK_TIMEOUT_S,
            )
            count = result.scalar()
            if count and count > 0:
                return {"status": "ok", "current_partition": f"spans_{suffix}"}
            return {
                "status": "failed",
                "reason": f"no spans partition for current month ({suffix})",
            }
    except Exception as exc:
        return {"status": "failed", "reason": str(exc)}


def _check_background_task(state: Any, attr: str) -> dict[str, Any]:
    """Verify a background task on ``app.state`` is still running.

    The receiver's lifespan starts three long-running asyncio tasks
    (retention sweep, webhook delivery sweeper, budget monitor) that
    drive critical periodic work. If any of them dies — typically by
    raising an unhandled exception — the receiver is still serving
    HTTP but is silently degraded: budgets stop enforcing, dead-letter
    webhooks stop draining, expired data stops being cleaned up.
    Readiness should reflect that.

    Dominant failure mode is ``task.done() and task.exception() is not
    None``. We surface the exception type in the reason so operators see
    immediately what crashed without grepping logs.

    Hang detection (task still running but stuck) would need explicit
    heartbeat state on each loop and is intentionally out of scope here.
    """
    task = getattr(state, attr, None)
    if task is None:
        return {
            "status": "failed",
            "reason": f"task {attr} not registered on app.state (lifespan startup failed?)",
        }

    if not task.done():
        return {"status": "ok"}

    # Task finished. Determine why so the operator gets a useful reason.
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return {"status": "failed", "reason": f"task {attr} was cancelled"}
    except asyncio.InvalidStateError:
        # Task is done but exception() can't be read - shouldn't happen
        # given the done() guard above, but cover the case to avoid
        # crashing the probe.
        return {"status": "failed", "reason": f"task {attr} done but state unreadable"}

    if exc is not None:
        return {
            "status": "failed",
            "reason": f"task {attr} crashed with {type(exc).__name__}: {exc}",
        }

    # Task returned normally - background loops aren't supposed to do that.
    return {
        "status": "failed",
        "reason": f"task {attr} exited without an exception (background loops should run forever)",
    }

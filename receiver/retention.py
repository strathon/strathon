"""Background retention cleanup for the Strathon receiver.

Deletes traces (and via FK cascade, their spans / span_events / span_links /
policy_matches) older than each project's configured
``project_settings.trace_retention_days``.

After stage 4 of the ORM refactor:
- DB work lives in receiver/repositories/retention.py (uses AsyncSession)
- This module owns the loop + config + env-parsing
- The loop constructs its own session per sweep via async_session_maker

### Design

- Single background task loop owned by the FastAPI lifespan
- Runs every ``STRATHON_RETENTION_INTERVAL_SECONDS`` seconds (default: 3600)
- Honors a shutdown event so server stop is clean
- Per-project cutoff in nanoseconds: ``now - retention_days * 86400e9``
- Deletes traces in capped batches to avoid long-running transactions that
  would block ingest
- Counts deletions for /metrics exposure
- Disabled with ``STRATHON_RETENTION_ENABLED=false``

### Why not server-side cron / pg_cron extension

We want zero-extra-infrastructure deploys for v1. A single `uvicorn` process
running this background task covers the 95% case (single-receiver
deployments). For multi-receiver deployments operators should disable this
loop and run the same DELETE via a scheduled job — documented in the
retention docs.

### Multi-process caveat

If running under gunicorn with multiple workers, EACH worker runs the loop
which means redundant deletes. The deletes are idempotent (already-deleted
rows are no-ops) but log noise and DB load multiply. Documented; the proper
fix is to elect one leader via Postgres advisory locks (deferred to v2).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

# Re-export for back-compat with tests / external callers
from repositories.retention import NS_PER_DAY, cleanup_once  # noqa: F401

logger = logging.getLogger("strathon.receiver.retention")


DEFAULT_INTERVAL_SECONDS = 3600  # 1 hour
DEFAULT_BATCH_SIZE = 5000  # max traces deleted per project per pass


@dataclass(frozen=True)
class RetentionConfig:
    """Effective retention configuration for the receiver.

    Resolved from environment variables via ``from_env()``. Kept as a
    frozen dataclass rather than reading from settings on every loop
    iteration so the loop's behavior is fixed at startup time — operators
    changing env vars during runtime would expect a restart to apply.
    """

    enabled: bool
    interval_seconds: int
    batch_size: int

    @classmethod
    def from_env(cls) -> "RetentionConfig":
        """Build config from environment.

        Reads via the typed `config.settings` singleton when possible,
        falling back to os.getenv for env vars that the Settings object
        doesn't model yet. The env-driven validation in Settings already
        enforces the same `interval >= 60` floor and `batch_size >= 1`
        rule that this function previously enforced, so we just trust it.
        """
        # Import here so module import isn't coupled to settings init —
        # tests that don't have DATABASE_URL set would otherwise crash
        # on `import retention`.
        try:
            from config import settings
            return cls(
                enabled=settings.retention_enabled,
                interval_seconds=settings.retention_interval_seconds,
                batch_size=settings.retention_batch_size,
            )
        except Exception:
            # Fall through to legacy parsing — keeps `from_env()` working
            # in test contexts that set env vars directly without going
            # through the Settings singleton.
            pass

        enabled = (os.getenv("STRATHON_RETENTION_ENABLED", "true").lower()
                   not in ("false", "0", "no", "off"))

        raw_interval = os.getenv("STRATHON_RETENTION_INTERVAL_SECONDS")
        interval = DEFAULT_INTERVAL_SECONDS
        if raw_interval is not None:
            try:
                interval = max(60, int(raw_interval))  # don't hammer the DB
            except ValueError:
                logger.warning(
                    "STRATHON_RETENTION_INTERVAL_SECONDS=%r not an int; using default %d",
                    raw_interval, DEFAULT_INTERVAL_SECONDS,
                )

        raw_batch = os.getenv("STRATHON_RETENTION_BATCH_SIZE")
        batch = DEFAULT_BATCH_SIZE
        if raw_batch is not None:
            try:
                batch = max(1, int(raw_batch))
            except ValueError:
                logger.warning(
                    "STRATHON_RETENTION_BATCH_SIZE=%r not an int; using default %d",
                    raw_batch, DEFAULT_BATCH_SIZE,
                )

        return cls(enabled=enabled, interval_seconds=interval, batch_size=batch)


async def retention_loop(
    config: RetentionConfig,
    shutdown_event: asyncio.Event,
    metrics_counters=None,
) -> None:
    """Run the periodic retention sweep until shutdown is signaled.

    ``metrics_counters`` is an optional ``RetentionCounters``-shaped object
    (see receiver/metrics.py) that gets incremented after each sweep.
    Passed in rather than imported to avoid circular dependencies.

    Each sweep opens its own AsyncSession via async_session_maker, runs
    cleanup_once against it, and commits. If a sweep fails the session is
    rolled back automatically by the context manager; the next interval
    starts a fresh session.
    """
    if not config.enabled:
        logger.info("Retention loop disabled (STRATHON_RETENTION_ENABLED=false)")
        return

    logger.info(
        "Retention loop starting: interval=%ds, batch_size=%d",
        config.interval_seconds, config.batch_size,
    )

    # First run after a short delay so we don't compete with startup activity
    initial_delay = min(30, config.interval_seconds)
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=initial_delay)
        return  # shutdown signaled during initial delay
    except asyncio.TimeoutError:
        pass

    while not shutdown_event.is_set():
        try:
            result = await _run_sweep(config.batch_size)
            if metrics_counters is not None:
                metrics_counters.record_sweep(
                    projects_scanned=result["projects_scanned"],
                    traces_deleted=result["traces_deleted"],
                )
            logger.debug(
                "retention sweep complete: %d projects scanned, %d traces deleted",
                result["projects_scanned"], result["traces_deleted"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Log and continue — a single failed sweep shouldn't kill the loop
            logger.exception("retention sweep failed; will retry next interval")
            if metrics_counters is not None:
                metrics_counters.record_sweep_error()

        # Sleep until the next interval OR shutdown
        try:
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=config.interval_seconds
            )
            return  # shutdown signaled
        except asyncio.TimeoutError:
            continue  # interval elapsed, run another sweep

    logger.info("Retention loop exited")


async def _run_sweep(batch_size: int) -> dict[str, int]:
    """Open a session, run one cleanup pass, commit, return the result.

    Separated from retention_loop so tests can exercise this path with a
    real DB without driving the timing of the outer loop.
    """
    # Imported here so the module-level import graph stays clean — the
    # retention_loop config-disabled path mustn't pull in the DB engine.
    from database import async_session_maker

    async with async_session_maker() as session:
        result = await cleanup_once(session, batch_size=batch_size)
        await session.commit()
        return result


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_INTERVAL_SECONDS",
    "NS_PER_DAY",
    "RetentionConfig",
    "cleanup_once",
    "retention_loop",
]

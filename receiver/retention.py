"""Background retention cleanup for the Strathon receiver.

Deletes traces (and via FK cascade, their spans / span_events / span_links /
policy_matches) older than each project's configured
``project_settings.trace_retention_days``.

### Design

- Single background task loop owned by the FastAPI lifespan
- Runs every ``STRATHON_RETENTION_INTERVAL_SECONDS`` seconds (default: 3600)
- Honors a shutdown event so server stop is clean
- Per-project cutoff in nanoseconds: ``now - retention_days * 86400e9``
- Deletes traces in capped batches to avoid long-running transactions that
  would block ingest
- Counts deletions for /metrics exposure
- Disabled with ``STRATHON_RETENTION_ENABLED=false``

### Why not a server-side cron / pg_cron extension

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
import time
from dataclasses import dataclass

import asyncpg

logger = logging.getLogger("strathon.receiver.retention")


DEFAULT_INTERVAL_SECONDS = 3600  # 1 hour
DEFAULT_BATCH_SIZE = 5000  # max traces deleted per project per pass
NS_PER_DAY = 86_400 * 1_000_000_000


@dataclass(frozen=True)
class RetentionConfig:
    """Effective retention configuration for the receiver."""

    enabled: bool
    interval_seconds: int
    batch_size: int

    @classmethod
    def from_env(cls) -> "RetentionConfig":
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


async def cleanup_once(
    pool: asyncpg.Pool, batch_size: int = DEFAULT_BATCH_SIZE
) -> dict[str, int]:
    """Run a single retention sweep across all projects.

    Returns a dict of metrics for the sweep:
        - projects_scanned: how many project_settings rows we evaluated
        - traces_deleted: total traces removed (spans cascade-delete from these)
    """
    now_ns = time.time_ns()

    async with pool.acquire() as conn:
        # Pull active retention windows for every project that has settings.
        settings_rows = await conn.fetch(
            """
            SELECT ps.project_id, ps.trace_retention_days
            FROM project_settings ps
            JOIN projects p ON p.id = ps.project_id
            WHERE p.deleted_at IS NULL
              AND ps.trace_retention_days > 0
            """
        )

        total_deleted = 0
        for row in settings_rows:
            project_id = row["project_id"]
            retention_days = row["trace_retention_days"]
            cutoff_ns = now_ns - retention_days * NS_PER_DAY

            # Capped delete: use a CTE so we never delete more than batch_size
            # rows in a single transaction. Long-running deletes can hold row
            # locks that block ingest INSERTs on the same traces. If a project
            # has > batch_size eligible rows, the next sweep will catch them.
            result = await conn.execute(
                """
                WITH expired AS (
                    SELECT id FROM traces
                    WHERE project_id = $1
                      AND start_time_unix_nano < $2
                    LIMIT $3
                )
                DELETE FROM traces
                WHERE id IN (SELECT id FROM expired)
                """,
                project_id, cutoff_ns, batch_size,
            )
            # asyncpg returns 'DELETE n'
            try:
                deleted = int(result.split(" ", 1)[1])
            except (IndexError, ValueError):
                deleted = 0

            if deleted > 0:
                logger.info(
                    "retention: deleted %d expired traces for project %s "
                    "(retention=%d days, cutoff=%d ns)",
                    deleted, project_id, retention_days, cutoff_ns,
                )
            total_deleted += deleted

    return {
        "projects_scanned": len(settings_rows),
        "traces_deleted": total_deleted,
    }


async def retention_loop(
    pool: asyncpg.Pool,
    config: RetentionConfig,
    shutdown_event: asyncio.Event,
    metrics_counters=None,
) -> None:
    """Run the periodic retention sweep until shutdown is signaled.

    ``metrics_counters`` is an optional ``RetentionCounters``-shaped object
    (see receiver/metrics.py) that gets incremented after each sweep.
    Passed in rather than imported to avoid circular dependencies.
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
            result = await cleanup_once(pool, batch_size=config.batch_size)
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


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_INTERVAL_SECONDS",
    "NS_PER_DAY",
    "RetentionConfig",
    "cleanup_once",
    "retention_loop",
]

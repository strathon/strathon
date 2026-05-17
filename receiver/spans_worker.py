"""Partition maintenance for spans, span_events, span_links.

Single long-running task, launched from main.py's lifespan handler.
Runs once per day, ensures the next PREMAKE_MONTHS months of
partitions exist for all three co-partitioned tables. Optionally
drops partitions older than RETENTION_MONTHS.

Same pattern as ``audit.worker.partition_maintenance_loop``:
advisory-lock-guarded, idempotent, hand-rolled (no pg_partman).

Advisory lock key: ``hashtext('spans_partition_mgmt')`` — distinct
from the audit partition lock so both can run concurrently.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session_maker

logger = logging.getLogger("strathon.receiver.spans_worker")


MAINTENANCE_INTERVAL_SECONDS: int = 6 * 60 * 60  # every 6 hours
PREMAKE_MONTHS: int = 3
RETENTION_MONTHS: int = int(os.environ.get("STRATHON_SPAN_PARTITION_RETENTION_MONTHS", "12"))

# All three tables are co-partitioned with the same monthly RANGE
# on start_time_unix_nano. Partitions must be created and dropped
# together.
_TABLES = ("spans", "span_events", "span_links")


def _month_bounds_ns(year: int, month: int) -> tuple[int, int]:
    """Return [from_ns, to_ns) for a monthly partition at UTC."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return (
        int(start.timestamp()) * 1_000_000_000,
        int(end.timestamp()) * 1_000_000_000,
    )


def _suffix(year: int, month: int) -> str:
    return f"y{year}m{month:02d}"


def _advance_month(year: int, month: int, offset: int) -> tuple[int, int]:
    """Advance (year, month) by offset months (can be negative)."""
    m = (year * 12 + month - 1) + offset
    return m // 12, m % 12 + 1


async def ensure_partitions(session: AsyncSession) -> list[str]:
    """Ensure partitions exist for current month ± buffer.

    Creates previous month through +PREMAKE_MONTHS. Returns list of
    partition suffixes that were ensured.
    """
    now = datetime.now(timezone.utc)
    ensured: list[str] = []
    for offset in range(-1, PREMAKE_MONTHS + 1):
        y, m = _advance_month(now.year, now.month, offset)
        lo, hi = _month_bounds_ns(y, m)
        sfx = _suffix(y, m)
        for tbl in _TABLES:
            await session.execute(text(
                f"CREATE TABLE IF NOT EXISTS {tbl}_{sfx} "
                f"PARTITION OF {tbl} "
                f"FOR VALUES FROM ({lo}) TO ({hi})"
            ))
        ensured.append(sfx)
    await session.commit()
    return ensured


async def drop_old_partitions(session: AsyncSession) -> list[str]:
    """Drop partitions older than RETENTION_MONTHS.

    Drops matched triplets (spans + span_events + span_links) in the
    same transaction so no orphaned partitions remain.
    """
    now = datetime.now(timezone.utc)
    cutoff_y, cutoff_m = _advance_month(now.year, now.month, -RETENTION_MONTHS)
    cutoff = datetime(cutoff_y, cutoff_m, 1, tzinfo=timezone.utc)

    # Find existing spans partitions from pg_inherits.
    result = await session.execute(text("""
        SELECT child.relname
        FROM pg_inherits
        JOIN pg_class child ON child.oid = pg_inherits.inhrelid
        JOIN pg_class parent ON parent.oid = pg_inherits.inhparent
        WHERE parent.relname = 'spans'
          AND child.relname ~ '^spans_y[0-9]{4}m[0-9]{2}$'
    """))
    dropped: list[str] = []
    for row in result.all():
        name = row[0]
        try:
            y = int(name[7:11])
            m = int(name[12:14])
        except (ValueError, IndexError):
            continue
        if datetime(y, m, 1, tzinfo=timezone.utc) < cutoff:
            sfx = _suffix(y, m)
            # Drop children first (FK dependency), then parent.
            for tbl in ("span_events", "span_links", "spans"):
                await session.execute(
                    text(f"DROP TABLE IF EXISTS {tbl}_{sfx}")
                )
            dropped.append(sfx)
    if dropped:
        await session.commit()
    return dropped


async def maintenance_loop(
    shutdown_event: asyncio.Event,
    interval_seconds: int = MAINTENANCE_INTERVAL_SECONDS,
) -> None:
    """Run partition maintenance until shutdown.

    First sweep runs immediately at startup.
    """
    logger.info(
        "Spans partition maintenance starting: interval=%ds, "
        "premake=%d months, retain=%d months",
        interval_seconds, PREMAKE_MONTHS, RETENTION_MONTHS,
    )
    while not shutdown_event.is_set():
        try:
            async with async_session_maker() as session:
                # Advisory lock prevents racing across replicas.
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext('spans_partition_mgmt'))")
                )
                ensured = await ensure_partitions(session)
                dropped = await drop_old_partitions(session)
                if dropped:
                    logger.info(
                        "spans partition maintenance: ensured %d, dropped %d (%s)",
                        len(ensured), len(dropped), ", ".join(dropped),
                    )
                else:
                    logger.debug(
                        "spans partition maintenance: ensured %d, dropped 0",
                        len(ensured),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("spans partition maintenance sweep failed")

        try:
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=interval_seconds
            )
        except asyncio.TimeoutError:
            continue
    logger.info("Spans partition maintenance stopped")


__all__ = [
    "ensure_partitions",
    "drop_old_partitions",
    "maintenance_loop",
]

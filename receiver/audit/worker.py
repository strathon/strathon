"""Background workers for the audit log.

Two long-running tasks, both launched from main.py's lifespan
handler alongside retention and the budget monitor:

- :func:`partition_maintenance_loop` — once per day, ensures the
  next three months of audit.events partitions exist. Idempotent
  via ``CREATE TABLE IF NOT EXISTS``. Cheap (single DDL per day),
  hand-rolled rather than relying on the pg_partman extension so
  self-hosters don't have to install one.

- :func:`anchor_sealer_loop` — once per
  ``audit_anchor_interval_seconds`` (default 60s), computes a
  Merkle root over events from the prior interval and inserts an
  audit.anchors row. Provides external integrity-proof points.

Both loops use the same shutdown_event / asyncio.create_task /
lifespan teardown pattern as ``retention_loop``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audit.hash_chain import merkle_root
from database import async_session_maker


logger = logging.getLogger("strathon.receiver.audit.worker")


# Partition maintenance is cheap; once a day is plenty. We still
# guard with a try/except so a single failure doesn't kill the loop.
PARTITION_MAINTENANCE_INTERVAL_SECONDS: int = 24 * 60 * 60

# How many future months to maintain. Three covers a worker outage
# of nearly three months without missing inserts.
PARTITION_LOOKAHEAD_MONTHS: int = 3


# --- Partition maintenance ---------------------------------------------------


async def ensure_future_partitions(session: AsyncSession) -> list[str]:
    """Ensure partitions exist for current month + lookahead months.

    Returns the list of partition names that were CREATEd (skipped
    over those that already existed). Pure idempotent: no harm in
    running this multiple times.
    """
    today = date.today()
    year, month = today.year, today.month
    created: list[str] = []
    for _ in range(PARTITION_LOOKAHEAD_MONTHS + 1):
        name = f"events_{year:04d}_{month:02d}"
        if month == 12:
            next_year, next_month = year + 1, 1
        else:
            next_year, next_month = year, month + 1
        from_date = date(year, month, 1).isoformat()
        to_date = date(next_year, next_month, 1).isoformat()
        # IF NOT EXISTS makes this idempotent. We don't try to
        # detect "already existed" vs "newly created" — the DDL is
        # cheap either way.
        await session.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS audit.{name} "
                f"PARTITION OF audit.events "
                f"FOR VALUES FROM ('{from_date}') TO ('{to_date}')"
            )
        )
        created.append(name)
        year, month = next_year, next_month
    await session.commit()
    return created


async def partition_maintenance_loop(
    shutdown_event: asyncio.Event,
    interval_seconds: int = PARTITION_MAINTENANCE_INTERVAL_SECONDS,
) -> None:
    """Run partition maintenance until shutdown.

    First sweep runs immediately at startup so a freshly-deployed
    receiver has partitions for the next several months before any
    audit traffic hits.
    """
    logger.info(
        "Audit partition maintenance loop starting: interval=%ds",
        interval_seconds,
    )
    while not shutdown_event.is_set():
        try:
            async with async_session_maker() as session:
                created = await ensure_future_partitions(session)
                logger.debug(
                    "audit partition maintenance: ensured %d partitions",
                    len(created),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("audit partition maintenance sweep failed")

        try:
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=interval_seconds
            )
        except asyncio.TimeoutError:
            continue
    logger.info("Audit partition maintenance loop stopped")


# --- Anchor sealer -----------------------------------------------------------


async def seal_anchor(session: AsyncSession) -> dict[str, int | bytes | None]:
    """Compute and insert one integrity anchor.

    Behavior:

    - Find the timestamp of the most recent anchor (``last_anchor_at``);
      use the epoch if there is none.
    - Select all events with ``occurred_at > last_anchor_at``, ordered
      by ``sequence_no`` ASC.
    - If no events are present, no anchor is written and the function
      returns a zero-count summary. We don't anchor empty intervals
      because that would produce gaps in the chain's Merkle proofs
      without adding integrity value.
    - Otherwise compute the Merkle root over the row_hash bytes and
      insert one audit.anchors row.

    Returns a summary dict for logging.
    """
    last_row = await session.execute(
        text(
            "SELECT anchor_at FROM audit.anchors "
            "ORDER BY anchor_at DESC LIMIT 1"
        )
    )
    last_anchor_at = last_row.scalar_one_or_none()
    if last_anchor_at is None:
        last_anchor_at = datetime(1970, 1, 1, tzinfo=timezone.utc)

    rows_result = await session.execute(
        text(
            "SELECT sequence_no, row_hash FROM audit.events "
            "WHERE occurred_at > :last "
            "ORDER BY sequence_no ASC"
        ),
        {"last": last_anchor_at},
    )
    rows = list(rows_result.all())
    if not rows:
        return {"event_count": 0, "merkle_root": None, "last_sequence": None}

    hashes = [bytes(r[1]) for r in rows]
    root = merkle_root(hashes)
    last_seq = rows[-1][0]
    last_hash = hashes[-1]
    now = datetime.now(timezone.utc)

    await session.execute(
        text(
            "INSERT INTO audit.anchors "
            "(anchor_at, last_sequence, last_row_hash, merkle_root, event_count) "
            "VALUES (:anchor_at, :last_seq, :last_hash, :root, :count)"
        ),
        {
            "anchor_at": now,
            "last_seq": last_seq,
            "last_hash": last_hash,
            "root": root,
            "count": len(rows),
        },
    )
    await session.commit()
    return {
        "event_count": len(rows),
        "merkle_root": root,
        "last_sequence": last_seq,
    }


async def anchor_sealer_loop(
    shutdown_event: asyncio.Event,
    interval_seconds: int = 60,
) -> None:
    """Run the anchor sealer until shutdown.

    First seal runs ``interval_seconds`` after startup so an early-
    morning bounce doesn't immediately overlap a previous seal. The
    sealer is single-instance; running two of them just makes
    redundant anchors (harmless but wasteful).
    """
    logger.info(
        "Audit anchor sealer starting: interval=%ds",
        interval_seconds,
    )

    # Initial delay so concurrent restarts don't pile up.
    try:
        await asyncio.wait_for(
            shutdown_event.wait(), timeout=interval_seconds
        )
        return
    except asyncio.TimeoutError:
        pass

    while not shutdown_event.is_set():
        try:
            async with async_session_maker() as session:
                summary = await seal_anchor(session)
                if summary["event_count"]:
                    logger.debug(
                        "audit anchor sealed: events=%d last_seq=%s",
                        summary["event_count"], summary["last_sequence"],
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("audit anchor seal failed")

        try:
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=interval_seconds
            )
        except asyncio.TimeoutError:
            continue
    logger.info("Audit anchor sealer stopped")


__all__ = [
    "anchor_sealer_loop",
    "ensure_future_partitions",
    "partition_maintenance_loop",
    "seal_anchor",
]

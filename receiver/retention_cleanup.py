"""Data retention cleanup task.

Runs daily. Deletes data older than the project's retention settings.
Never deletes audit entries (compliance requirement).

Cleans:
  - Spans older than retention_spans_days
  - Expired sessions
  - Expired API keys
  - Resolved approvals older than 90 days

Research: GDPR Article 5(1)(e) storage limitation principle,
Postgres partition DROP for efficient bulk deletion.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("strathon.retention")

CLEANUP_INTERVAL = int(os.environ.get("STRATHON_RETENTION_INTERVAL_HOURS", "24")) * 3600


async def retention_cleanup_loop(session_maker) -> None:
    """Background task that runs retention cleanup daily."""
    logger.info("Retention cleanup started (interval=%dh)",
                CLEANUP_INTERVAL // 3600)

    while True:
        try:
            async with session_maker() as session:
                deleted = await _run_cleanup(session)
            logger.info(
                "Retention cleanup complete: %d spans, %d sessions, "
                "%d expired keys, %d old approvals deleted",
                deleted["spans"], deleted["sessions"],
                deleted["keys"], deleted["approvals"],
            )
        except asyncio.CancelledError:
            logger.info("Retention cleanup shutting down")
            break
        except Exception:
            logger.exception("Retention cleanup failed")

        await asyncio.sleep(CLEANUP_INTERVAL)


async def _run_cleanup(session: AsyncSession) -> dict[str, int]:
    """Execute all cleanup tasks. Returns counts of deleted rows."""
    counts = {"spans": 0, "sessions": 0, "keys": 0, "approvals": 0}

    # 1. Delete expired sessions.
    result = await session.execute(text(
        "DELETE FROM sessions WHERE expires_at < NOW()"
    ))
    counts["sessions"] = result.rowcount or 0

    # 2. Delete expired API keys.
    result = await session.execute(text(
        "DELETE FROM api_keys WHERE expires_at IS NOT NULL AND expires_at < NOW()"
    ))
    counts["keys"] = result.rowcount or 0

    # 3. Delete old resolved approvals (>90 days).
    result = await session.execute(text(
        "DELETE FROM approvals WHERE status != 'pending' "
        "AND resolved_at < NOW() - INTERVAL '90 days'"
    ))
    counts["approvals"] = result.rowcount or 0

    # 4. Drop old span partitions based on retention.
    # Get all projects and their retention settings.
    projects = await session.execute(text("""
        SELECT p.id, COALESCE(ps.trace_retention_days, 30) AS retention_days
        FROM projects p
        LEFT JOIN project_settings ps ON ps.project_id = p.id
        WHERE p.deleted_at IS NULL
    """))

    for row in projects.all():
        retention_days = row[1]
        # Delete spans older than retention (partition DROP is more
        # efficient but requires knowing partition boundaries).
        result = await session.execute(text(
            "DELETE FROM spans WHERE project_id = :pid "
            "AND start_time_unix_nano < "
            "EXTRACT(EPOCH FROM NOW() - make_interval(days => :days))::BIGINT * 1000000000"
        ), {"pid": row[0], "days": retention_days})
        counts["spans"] += result.rowcount or 0

    # NEVER delete audit entries. Compliance requirement.

    await session.commit()
    return counts

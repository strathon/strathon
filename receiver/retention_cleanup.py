"""Data retention cleanup task.

Runs daily. Deletes auxiliary data older than retention settings.
Never deletes audit entries (compliance requirement).

Cleans:
  - Expired sessions
  - Expired API keys
  - Resolved approvals older than 90 days

Span retention is handled separately by the partition-based
``retention.retention_loop`` (efficient partition drops, honors shutdown,
emits metrics) — this loop intentionally does not delete spans.

Research: GDPR Article 5(1)(e) storage limitation principle.
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
                "Retention cleanup complete: %d sessions, "
                "%d expired keys, %d old approvals deleted",
                deleted["sessions"], deleted["keys"], deleted["approvals"],
            )
        except asyncio.CancelledError:
            logger.info("Retention cleanup shutting down")
            break
        except Exception:
            logger.exception("Retention cleanup failed")

        await asyncio.sleep(CLEANUP_INTERVAL)


async def _run_cleanup(session: AsyncSession) -> dict[str, int]:
    """Execute all cleanup tasks. Returns counts of deleted rows.

    Spans are intentionally NOT handled here — span retention is owned by the
    partition-based ``retention.retention_loop`` (efficient partition drops,
    honors shutdown, emits metrics). This loop covers the auxiliary tables that
    loop does not: sessions, API keys, and old resolved approvals. Audit entries
    are never deleted (compliance requirement).
    """
    counts = {"sessions": 0, "keys": 0, "approvals": 0}

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

    # NEVER delete audit entries. Compliance requirement.

    await session.commit()
    return counts

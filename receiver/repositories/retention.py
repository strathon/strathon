"""Retention sweep — delete expired traces, scoped per project.

The single DB operation `cleanup_once` lives here. The loop driver,
`RetentionConfig`, and env parsing stay in `receiver/retention.py`
because they're orchestration, not persistence.

Transaction model:
    Callers pass an AsyncSession. The function executes the per-project
    DELETEs against that session but never commits — same rule as the
    other repositories. The lifecycle owner (the retention loop's
    `async with async_session_maker()` block) handles commit.

    Note: because every project's DELETE shares one transaction, a
    project-level failure will roll back all earlier projects too. For
    v1 this is acceptable — cleanup is idempotent so the next sweep
    retries the lot. If sweeps grow expensive we can move to one
    transaction per project.

Why CTE-bounded DELETE:
    Long-running DELETEs hold row locks that block ingest INSERTs on
    the same trace rows. Capping the per-pass delete at `batch_size`
    keeps any single transaction short. If a project has more eligible
    rows than `batch_size`, the next sweep picks up where this one
    left off.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("strathon.receiver.repositories.retention")


NS_PER_DAY = 86_400 * 1_000_000_000


async def cleanup_once(
    session: AsyncSession,
    batch_size: int,
) -> dict[str, int]:
    """Run a single retention sweep across all projects on this session.

    Returns:
        {
            "projects_scanned": int,
            "traces_deleted":   int,
        }

    The caller is responsible for committing the transaction. Errors
    propagate so the caller can decide whether to rollback or retry.
    """
    now_ns = time.time_ns()

    # We use raw SQL via text() here for two reasons:
    #   1. The DELETE-with-CTE is a Postgres-specific pattern that the
    #      ORM's emit_delete doesn't express as cleanly.
    #   2. This is the only spot in the receiver that benefits from staying
    #      close to the SQL surface — retention is a maintenance op, not
    #      part of any domain model.
    settings_result = await session.execute(
        text(
            """
            SELECT ps.project_id, ps.trace_retention_days
            FROM project_settings ps
            JOIN projects p ON p.id = ps.project_id
            WHERE p.deleted_at IS NULL
              AND ps.trace_retention_days > 0
            """
        )
    )
    settings_rows = settings_result.all()

    total_deleted = 0
    for row in settings_rows:
        project_id = row.project_id
        retention_days = row.trace_retention_days
        cutoff_ns = now_ns - retention_days * NS_PER_DAY

        delete_result = await session.execute(
            text(
                """
                WITH expired AS (
                    SELECT id FROM traces
                    WHERE project_id = :project_id
                      AND start_time_unix_nano < :cutoff
                    LIMIT :batch_size
                )
                DELETE FROM traces
                WHERE id IN (SELECT id FROM expired)
                """
            ),
            {
                "project_id": project_id,
                "cutoff": cutoff_ns,
                "batch_size": batch_size,
            },
        )
        deleted = delete_result.rowcount or 0

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

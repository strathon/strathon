"""Background task: expire pending approvals past their timeout.

Runs on a periodic tick (default 30s). Marks pending approvals as
expired when expires_at < now. Same pattern as key_reaper.py.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("strathon.receiver.approval_reaper")


async def _tick(session_maker) -> None:
    import repositories.approvals as approvals_repo

    async with session_maker() as session:
        try:
            count = await approvals_repo.expire_pending_approvals(session)
            await session.commit()
            if count:
                logger.info("Expired %d pending approval(s)", count)
        except Exception:
            logger.exception("approval reaper: expire failed")
            await session.rollback()


async def approval_reaper_loop(
    session_maker,
    interval_seconds: int = 30,
) -> None:
    """Run the approval reaper on a periodic tick until cancelled."""
    logger.info("Approval reaper started (interval=%ds)", interval_seconds)
    while True:
        try:
            await _tick(session_maker)
        except asyncio.CancelledError:
            logger.info("Approval reaper cancelled")
            return
        except Exception:
            logger.exception("approval reaper tick failed")
        await asyncio.sleep(interval_seconds)

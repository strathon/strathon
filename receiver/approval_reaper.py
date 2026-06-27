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
            expired = await approvals_repo.expire_pending_approvals(session)
            await session.commit()
        except Exception:
            logger.exception("approval reaper: expire failed")
            await session.rollback()
            return

        # Best-effort auto-denied notification to each project's configured
        # channels. The approvals already failed closed above; a notification
        # failure must never affect that, so it is fully isolated.
        if not expired:
            return
        try:
            from config import get_settings
            from integrations.dispatcher import dispatch_event

            base_url = get_settings().public_url
            for appr in expired:
                await dispatch_event(
                    session,
                    appr["project_id"],
                    "approval_expired",
                    {
                        "approval_id": appr["id"],
                        "agent_name": appr["agent_name"] or "agent",
                        "tool_name": appr["tool_name"] or "unknown",
                        "policy_name": appr["policy_name"] or "unknown",
                    },
                    base_url=base_url,
                )
        except Exception:
            logger.exception("approval reaper: expiry notification failed")


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

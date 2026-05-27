"""Background task: reap expired API keys and warn about near-expiry keys.

Runs on a periodic tick (configurable, default 300s). Two actions per tick:

1. **Reap**: revoke all keys past their expires_at. This is the
   authoritative cleanup — the auth hot path also rejects expired keys
   in the application layer, so the reaper just ensures the DB state
   is consistent and the partial indexes stay clean.

2. **Warn**: find keys expiring within 24h that haven't been warned
   about yet. Emit a webhook event for each. Uses a simple
   module-level set to track warned key IDs within a process lifetime
   (no DB column needed — if the process restarts, re-warning is
   harmless and preferred over missing a warning).

Follows the same pattern as budget_monitor.py and audit/worker.py:
asyncio.create_task in the lifespan, advisory-lock-guarded if needed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Set
from uuid import UUID

logger = logging.getLogger("strathon.receiver.key_reaper")

# Track which keys we've already warned about (within this process).
_warned_key_ids: Set[UUID] = set()


async def _tick(session_maker, warn_hours: int = 24) -> None:
    """Single tick of the reaper loop."""
    import repositories.auth as auth_repo

    async with session_maker() as session:
        # 1. Reap expired keys.
        try:
            count = await auth_repo.reap_expired_keys(session)
            await session.commit()
            if count:
                logger.info("Reaped %d expired API key(s)", count)
        except Exception:
            logger.exception("key reaper: reap failed")
            await session.rollback()

    async with session_maker() as session:
        # 2. Warn about keys expiring soon.
        try:
            expiring = await auth_repo.find_keys_expiring_soon(
                session, within_hours=warn_hours
            )
            for key in expiring:
                if key.id in _warned_key_ids:
                    continue
                _warned_key_ids.add(key.id)
                logger.warning(
                    "API key %s (prefix %s, project %s) expires at %s",
                    key.id,
                    key.key_prefix,
                    key.project_id,
                    key.expires_at,
                )
                # Key expiry notification via notification dispatcher.
                # event_type="api_key.expiring_soon" once the webhook
                # dispatch supports non-policy event types.
        except Exception:
            logger.exception("key reaper: warn scan failed")


async def reaper_loop(
    session_maker,
    interval_seconds: int = 300,
    warn_hours: int = 24,
) -> None:
    """Run the reaper on a periodic tick until cancelled."""
    logger.info(
        "Key reaper started (interval=%ds, warn_hours=%d)",
        interval_seconds,
        warn_hours,
    )
    while True:
        try:
            await _tick(session_maker, warn_hours=warn_hours)
        except asyncio.CancelledError:
            logger.info("Key reaper cancelled")
            return
        except Exception:
            logger.exception("key reaper tick failed")
        await asyncio.sleep(interval_seconds)

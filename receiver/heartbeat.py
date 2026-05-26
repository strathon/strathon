"""Heartbeat monitoring for agent liveness detection.

Tracks when each agent last sent a heartbeat span. If no heartbeat
is received for 2 minutes (configurable), fires an alert through
the notification dispatcher.

SDK sends heartbeat spans with name "strathon.heartbeat" every 30s.
The traces ingest intercepts these and updates the tracker instead
of storing them as regular spans.

Research: Kubernetes liveness probes, Consul health checks,
Netflix Eureka heartbeat pattern.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

logger = logging.getLogger("strathon.heartbeat")

HEARTBEAT_TIMEOUT = int(os.environ.get("STRATHON_HEARTBEAT_TIMEOUT_SECONDS", "120"))
CHECK_INTERVAL = 30

# In-memory tracker: agent_name → last heartbeat timestamp (monotonic).
_last_heartbeat: dict[str, float] = {}
# Track which agents we already alerted for (avoid repeated alerts).
_alerted: set[str] = set()


def record_heartbeat(agent_name: str, attrs: dict[str, Any] | None = None) -> None:
    """Record a heartbeat from an agent."""
    _last_heartbeat[agent_name] = time.monotonic()
    if agent_name in _alerted:
        _alerted.discard(agent_name)
        logger.info("Agent '%s' heartbeat resumed", agent_name)


def is_heartbeat_span(span_name: str) -> bool:
    """Check if a span is a heartbeat (should not be stored)."""
    return span_name == "strathon.heartbeat"


async def heartbeat_check_loop(session_maker) -> None:
    """Background task checking for missed heartbeats.

    Runs every CHECK_INTERVAL seconds. If an agent hasn't sent
    a heartbeat within HEARTBEAT_TIMEOUT, fires an alert.
    """
    logger.info(
        "Heartbeat monitor started (timeout=%ds, check=%ds)",
        HEARTBEAT_TIMEOUT, CHECK_INTERVAL,
    )

    while True:
        try:
            now = time.monotonic()
            for agent_name, last_seen in list(_last_heartbeat.items()):
                elapsed = now - last_seen
                if elapsed > HEARTBEAT_TIMEOUT and agent_name not in _alerted:
                    _alerted.add(agent_name)
                    logger.warning(
                        "Agent '%s' heartbeat missed (last seen %.0fs ago)",
                        agent_name, elapsed,
                    )
                    # Fire alert through notification dispatcher.
                    try:
                        from integrations.dispatcher import dispatch_event
                        async with session_maker() as session:
                            await dispatch_event(
                                session, None,
                                "heartbeat_missed", {
                                    "agent_name": agent_name,
                                    "last_seen_seconds_ago": round(elapsed),
                                    "timeout_seconds": HEARTBEAT_TIMEOUT,
                                    "severity": "high",
                                    "message": (
                                        f"Agent '{agent_name}' has not sent a heartbeat "
                                        f"for {round(elapsed)}s (timeout: {HEARTBEAT_TIMEOUT}s). "
                                        "The agent may have crashed or the SDK may have been bypassed."
                                    ),
                                },
                            )
                    except Exception:
                        logger.exception("Failed to dispatch heartbeat alert")

        except asyncio.CancelledError:
            logger.info("Heartbeat monitor shutting down")
            break
        except Exception:
            logger.exception("Heartbeat check failed")

        await asyncio.sleep(CHECK_INTERVAL)

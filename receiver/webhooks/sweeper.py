"""Orphan-pending sweeper.

The webhook delivery architecture promises: if a row is durably
written but its Dramatiq message never lands (Redis was unreachable
during dispatch, the receiver crashed between row insert and message
send, the message landed but the worker died before consuming it),
the row stays ``pending`` and a sweeper task reclaims it later.

This module is that promise.

How it works
============

A background asyncio task runs on a fixed interval. Each tick:

  1. Open a session.
  2. Query for ``pending`` rows where ``next_attempt_at`` is older
     than NOW() - threshold_seconds.
  3. For each such row, dispatch a Dramatiq message.

Threshold tuning
================

Small threshold (e.g. 60s): aggressive. May double-dispatch a message
that's legitimately in flight but slow. This is safe — the actor's
first action is a SELECT-on-current-status that returns cleanly if the
row is no longer pending — but it spends Dramatiq queue capacity.

Large threshold (e.g. 600s): conservative. The recovery window is
longer (a Redis outage leaves rows pending for ten minutes before the
sweeper steps in). For the v1 default we use 300s (5 min). Operators
can tune via STRATHON_WEBHOOK_SWEEPER_THRESHOLD_SEC.

Multi-process caveat
====================

If running under gunicorn with multiple workers, each worker runs the
sweeper. Concurrent sweeps may try to re-dispatch the same row. This
is safe (idempotent at the actor) but wasteful. The proper fix is a
Postgres advisory lock electing one sweeper per cluster; deferred to
v2 alongside the same fix for the retention loop.

Lifecycle
=========

Started from main.py's lifespan; stopped when the shutdown event fires
on receiver shutdown. The loop wakes either on the interval timer or
when the shutdown event is set, whichever comes first, so a SIGINT
doesn't have to wait up to ``interval_seconds`` to drain.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from repositories import webhook_deliveries as deliveries_repo

logger = logging.getLogger("strathon.receiver.webhooks.sweeper")


DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_THRESHOLD_SECONDS = 300
DEFAULT_BATCH_LIMIT = 100


@dataclass(frozen=True)
class SweeperConfig:
    enabled: bool
    interval_seconds: int
    threshold_seconds: int
    batch_limit: int

    @classmethod
    def from_env(cls) -> "SweeperConfig":
        """Read configuration from environment.

        STRATHON_WEBHOOK_SWEEPER_ENABLED       false/0/no/off to disable
        STRATHON_WEBHOOK_SWEEPER_INTERVAL_SEC  loop tick (default 60)
        STRATHON_WEBHOOK_SWEEPER_THRESHOLD_SEC orphan age (default 300)
        STRATHON_WEBHOOK_SWEEPER_BATCH         max rows per tick (default 100)
        """
        enabled_raw = os.getenv("STRATHON_WEBHOOK_SWEEPER_ENABLED", "true").lower()
        enabled = enabled_raw not in ("false", "0", "no", "off")

        def _int_env(name: str, default: int) -> int:
            raw = os.getenv(name)
            if not raw:
                return default
            try:
                v = int(raw)
            except ValueError:
                logger.warning(
                    "%s is not an integer; using default %s", name, default,
                )
                return default
            return v if v > 0 else default

        return cls(
            enabled=enabled,
            interval_seconds=_int_env(
                "STRATHON_WEBHOOK_SWEEPER_INTERVAL_SEC", DEFAULT_INTERVAL_SECONDS,
            ),
            threshold_seconds=_int_env(
                "STRATHON_WEBHOOK_SWEEPER_THRESHOLD_SEC", DEFAULT_THRESHOLD_SECONDS,
            ),
            batch_limit=_int_env(
                "STRATHON_WEBHOOK_SWEEPER_BATCH", DEFAULT_BATCH_LIMIT,
            ),
        )


class SweeperMetrics:
    """Lightweight stats container.

    Held on app.state.metrics in main.py — the actual Prometheus
    counters live in StrathonMetrics. This class just gives the
    sweeper loop a typed surface for emitting events.
    """

    def __init__(self, metrics) -> None:
        self._metrics = metrics

    def record_sweep(self, reclaimed: int) -> None:
        self._metrics.webhook_sweeper_runs.inc()
        if reclaimed > 0:
            self._metrics.webhook_sweeper_reclaimed.inc(reclaimed)

    def record_sweep_error(self) -> None:
        self._metrics.webhook_sweeper_errors.inc()


async def sweep_once(
    session_maker: async_sessionmaker,
    *,
    threshold_seconds: int,
    batch_limit: int,
) -> int:
    """Run a single sweep tick. Returns the number of rows re-dispatched.

    Pulled out as a free function so tests can drive it directly without
    spinning up the loop.
    """
    async with session_maker() as session:
        ids = await deliveries_repo.find_orphan_pending_deliveries(
            session,
            threshold_seconds=threshold_seconds,
            limit=batch_limit,
        )

    if not ids:
        return 0

    # Dispatch outside the session — Dramatiq sends are I/O against
    # Redis, no DB transaction needed. Failures are best-effort; the
    # next sweep tick reclaims anything we didn't dispatch this time.
    from webhooks.actor import deliver_webhook

    dispatched = 0
    for delivery_id in ids:
        try:
            deliver_webhook.send(str(delivery_id))
            dispatched += 1
        except Exception:
            logger.exception(
                "sweeper failed to dispatch delivery %s; will retry on next tick",
                delivery_id,
            )
    if dispatched:
        logger.info("Sweeper re-dispatched %d orphan delivery row(s)", dispatched)
    return dispatched


async def sweeper_loop(
    config: SweeperConfig,
    shutdown_event: asyncio.Event,
    *,
    session_maker: async_sessionmaker,
    metrics: SweeperMetrics | None = None,
) -> None:
    """Run the sweeper in a loop until the shutdown event fires.

    Sleeps interruptibly between ticks: a shutdown_event.set() during
    a sleep causes the loop to exit within milliseconds, not after
    ``interval_seconds``. This matters for graceful uvicorn shutdown.
    """
    if not config.enabled:
        logger.info("Webhook sweeper disabled (STRATHON_WEBHOOK_SWEEPER_ENABLED=false)")
        return

    logger.info(
        "Webhook sweeper started (interval=%ds threshold=%ds batch=%d)",
        config.interval_seconds, config.threshold_seconds, config.batch_limit,
    )

    while not shutdown_event.is_set():
        try:
            reclaimed = await sweep_once(
                session_maker,
                threshold_seconds=config.threshold_seconds,
                batch_limit=config.batch_limit,
            )
            if metrics is not None:
                metrics.record_sweep(reclaimed)
        except Exception:
            logger.exception("Sweeper tick failed; will retry next interval")
            if metrics is not None:
                metrics.record_sweep_error()

        # Interruptible sleep
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=config.interval_seconds,
            )
        except asyncio.TimeoutError:
            continue  # interval elapsed, run next tick

    logger.info("Webhook sweeper stopped")


__all__ = [
    "DEFAULT_BATCH_LIMIT",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_THRESHOLD_SECONDS",
    "SweeperConfig",
    "SweeperMetrics",
    "sweep_once",
    "sweeper_loop",
]

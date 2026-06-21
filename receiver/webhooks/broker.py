"""Dramatiq broker setup for the webhook delivery queue.

The broker is initialized lazily — importing this module does not
connect to Redis. We do this so:

  * Importing ``receiver.webhooks`` from a test fixture does not require
    a running Redis. Tests that want real queue behavior configure
    Dramatiq's StubBroker themselves; tests that exercise signing and
    persistence don't need any broker at all.

  * The receiver process can boot when Redis is briefly unavailable.
    The dispatcher's enqueue_delivery() will see the connection failure
    and fall back to writing the row as pending; the sweeper actor will
    pick it up once Redis recovers.

Configuration: the broker URL is read from ``settings.webhook_redis_url``
(env STRATHON_WEBHOOK_REDIS_URL). If unset, the StubBroker is used,
which executes actor calls inline and is the right default for local
development and CI: signing and durability still happen end-to-end;
only the "send happens out of band" property is missing.

Production setups must set STRATHON_WEBHOOK_REDIS_URL explicitly. The
deploy docs spell out why: without a real broker, alert delivery
becomes synchronous on the OTLP ingest hot path and bursts of alerts
can serialize ingest latency.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import dramatiq
from dramatiq.brokers.stub import StubBroker

logger = logging.getLogger("strathon.receiver.webhooks.broker")


# Module-level singletons guarded by a lock so concurrent imports can't
# create two brokers. We don't expose the broker directly; callers use
# get_broker() which is the supported surface.
_broker: Optional[dramatiq.Broker] = None
_broker_lock = threading.Lock()


def get_broker() -> dramatiq.Broker:
    """Return the configured Dramatiq broker, initializing it on first call.

    Subsequent calls return the same broker. Tests can call
    ``reset_broker_for_testing()`` between cases to install a fresh
    StubBroker — without it, actor messages from one test would leak
    into the next.
    """
    global _broker
    if _broker is not None:
        return _broker
    with _broker_lock:
        if _broker is not None:
            return _broker
        _broker = _build_broker()
        dramatiq.set_broker(_broker)
    return _broker


def _build_broker() -> dramatiq.Broker:
    """Construct the broker the current configuration calls for.

    Reads settings lazily inside this function so importing webhooks.broker
    does not trigger settings loading at module import time.
    """
    from config import get_settings

    settings = get_settings()
    url = getattr(settings, "webhook_redis_url", None)

    if not url:
        logger.info(
            "STRATHON_WEBHOOK_REDIS_URL is not set; using StubBroker. "
            "Alert webhooks will be delivered inline on ingest. "
            "Set STRATHON_WEBHOOK_REDIS_URL to enable async delivery."
        )
        return StubBroker()

    # Import here so the production import path doesn't have to resolve
    # the redis client when we're in stub mode (cuts a ~5MB import from
    # the test runner's startup).
    from dramatiq.brokers.redis import RedisBroker

    logger.info("Initializing Dramatiq RedisBroker at %s", url)
    return RedisBroker(url=url)


def reset_broker_for_testing() -> None:
    """Reset the module-level broker singleton. Tests only.

    Each test that touches the queue should call this in setup so prior
    tests' messages don't bleed into it. Production code never calls this.
    """
    global _broker
    with _broker_lock:
        _broker = None


__all__ = ["get_broker", "reset_broker_for_testing"]

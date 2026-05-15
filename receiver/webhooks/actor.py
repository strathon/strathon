"""Dramatiq actor that performs a single webhook delivery attempt.

The actor is intentionally thin: build headers, POST, classify response,
update the DB row. Retry mechanics live in Dramatiq's built-in Retries
middleware — we don't reimplement backoff or jitter, we just configure
``max_retries`` / ``min_backoff`` / ``max_backoff`` on the actor and
raise the right kind of exception for the response we got.

Response classification
=======================

  HTTP 2xx                       -> mark succeeded, return (no retry)
  HTTP 429 with Retry-After      -> raise ``RetryAfter`` (Dramatiq honors)
  HTTP 5xx, timeout, conn error  -> raise ``RetriableError`` (backoff retry)
  HTTP 4xx other than 429        -> mark abandoned, return (no retry)
                                    A 400/401/404 won't fix itself with
                                    more attempts; we surface to operator
                                    via the dlq endpoint and stop trying.
  HTTP 3xx                       -> mark abandoned, return (no retry)
                                    Following redirects is a known
                                    security hole in webhook senders;
                                    Standard Webhooks operational
                                    guidance: treat 3xx as terminal,
                                    surface URL to operator.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import dramatiq
import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session_maker
from models.webhooks import WebhookDelivery
from webhooks.broker import get_broker
from webhooks.signing import compute_signature_headers

logger = logging.getLogger("strathon.receiver.webhooks.actor")


# Initialise the broker before declaring actors. Dramatiq actors register
# themselves with the global broker at decoration time, so the broker has
# to exist first.
get_broker()


class _RetriableDeliveryError(Exception):
    """5xx, timeout, or connection error. Triggers Dramatiq retry."""


def _classify(response_status: int) -> str:
    """Map an HTTP status to a delivery outcome.

    Returns 'succeeded', 'retriable', or 'abandoned'.
    """
    if 200 <= response_status < 300:
        return "succeeded"
    if response_status == 429:
        return "retriable"
    if 500 <= response_status < 600:
        return "retriable"
    # Everything else (3xx, 4xx other than 429) is non-retriable.
    return "abandoned"


async def _send_one(
    session: AsyncSession,
    delivery_id: str,
    *,
    request_timeout_sec: float,
) -> str:
    """Send a single delivery attempt. Return the final status string.

    All DB writes happen inside this function so the caller (the actor)
    sees a fully-up-to-date row when it returns.
    """
    # Load the delivery and the project's active signing keys in one round-trip.
    delivery = await session.scalar(
        select(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
    )
    if delivery is None:
        logger.warning("webhook delivery %s not found; nothing to send", delivery_id)
        return "missing"

    if delivery.status in ("succeeded", "dlq", "abandoned"):
        # Idempotent: a duplicate enqueue (e.g. sweeper + ingest race)
        # should not re-fire a terminal delivery.
        logger.info(
            "webhook delivery %s already terminal (%s); skipping",
            delivery_id, delivery.status,
        )
        return delivery.status

    # Fetch all active (non-revoked) signing keys for the project. We do
    # NOT have plaintext anymore; we only have hashes. So how do we sign?
    # The plaintext lives in the operator's hands, not in the database.
    # We store the plaintext for actively-used keys in an in-memory cache
    # populated when keys are created (see signing-key creation endpoint).
    # For freshly-restarted receivers that lost the cache, signing is
    # skipped until the operator re-supplies the plaintext or rotates.
    #
    # In the typical case the cache has the plaintexts populated at boot
    # from a one-time-use admin endpoint. This is the same trade-off
    # Stripe makes: secrets are not recoverable from server storage
    # after creation. (See webhooks/signing.py module docstring.)
    from webhooks.keystore import get_active_secrets

    project_secrets = get_active_secrets(delivery.project_id)

    import json
    body = json.dumps(delivery.payload, default=str, separators=(",", ":"))
    headers = compute_signature_headers(
        secrets_plaintext=project_secrets,
        webhook_id=delivery.webhook_id,
        body=body,
    )
    headers["content-type"] = "application/json"
    headers["user-agent"] = "Strathon-Receiver/1.0 (+strathon.io)"

    response_status: int | None = None
    error_message: str | None = None

    try:
        async with httpx.AsyncClient(timeout=request_timeout_sec) as client:
            resp = await client.post(delivery.url, content=body, headers=headers)
            response_status = resp.status_code
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        outcome = "retriable"
    except httpx.HTTPError as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        outcome = "retriable"
    else:
        outcome = _classify(response_status)

    # Update the row according to the outcome.
    now = datetime.now(timezone.utc)
    new_attempts = delivery.attempts + 1

    if outcome == "succeeded":
        new_status = "succeeded"
    elif outcome == "abandoned":
        new_status = "abandoned"
    else:  # retriable
        if new_attempts >= delivery.max_attempts:
            new_status = "dlq"
        else:
            new_status = "failed_retrying"

    await session.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == delivery.id)
        .values(
            status=new_status,
            attempts=new_attempts,
            last_attempt_at=now,
            last_response_status=response_status,
            last_error=error_message,
        )
    )
    await session.commit()

    # For retriable outcomes that aren't yet at dlq, raise so Dramatiq's
    # Retries middleware schedules a retry with backoff. The middleware
    # owns the delay calculation; we just signal "try again."
    if outcome == "retriable" and new_status == "failed_retrying":
        raise _RetriableDeliveryError(
            f"webhook {delivery.webhook_id} attempt {new_attempts} failed: "
            f"status={response_status} error={error_message}"
        )

    return new_status


# Read settings inside a function so importing this module doesn't
# trigger Settings instantiation at import time.
def _actor_options() -> dict:
    from config import get_settings
    s = get_settings()
    return {
        "max_retries": s.webhook_max_attempts - 1,  # max_retries is *additional* attempts
        "min_backoff": s.webhook_min_backoff_ms,
        "max_backoff": s.webhook_max_backoff_ms,
    }


# Declared at module level so Dramatiq workers can import it by reference.
# Configuration is fixed at decoration time; if operators change the env
# vars they need to restart workers.
_opts = _actor_options()


@dramatiq.actor(
    queue_name="webhook_deliveries",
    max_retries=_opts["max_retries"],
    min_backoff=_opts["min_backoff"],
    max_backoff=_opts["max_backoff"],
)
def deliver_webhook(delivery_id: str) -> None:
    """Run one delivery attempt for the given webhook_deliveries.id.

    Dramatiq workers call this. The actor is sync at the Dramatiq layer
    (Dramatiq workers are process-based) but the actual HTTP send is
    async — we drive it with asyncio.run() inside the actor body.
    """
    import asyncio

    from config import get_settings
    s = get_settings()

    async def _go():
        sessionmaker = get_session_maker()
        async with sessionmaker() as session:
            return await _send_one(
                session, delivery_id,
                request_timeout_sec=s.webhook_request_timeout_sec,
            )

    asyncio.run(_go())


__all__ = ["deliver_webhook"]

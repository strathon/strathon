"""Webhook delivery enqueue.

Called from the OTLP ingest path for every matched ``alert``-action
policy. Inserts the durable ``webhook_deliveries`` row inside the
caller's DB transaction and arranges for the Dramatiq send to fire
once the transaction commits.

Atomicity guarantee
===================

Sending the Dramatiq message *before* the transaction commits would
risk a phantom delivery for a row that rolls back. Sending *during*
the commit hook eliminates the race: we use SQLAlchemy's
``after_commit`` session event so the message is only sent after the
durable write succeeds. If the commit rolls back, no message is sent,
and the in-flight `WebhookDelivery` object is invalidated by
SQLAlchemy automatically.

If Redis is unreachable at the moment of dispatch, the exception is
swallowed (logged) and the durable row stays in ``pending`` status;
the sweeper actor picks it up on a later tick. This is the whole
point of having the durable state separate from the queue.
"""

from __future__ import annotations

import logging
import secrets as _stdlib_secrets
import uuid
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from models.webhooks import WebhookDelivery

logger = logging.getLogger("strathon.receiver.webhooks.dispatch")


def _generate_webhook_id() -> str:
    """Standard Webhooks msg id format: 'msg_' + 22 url-safe random chars.

    22 base64url chars carry ~131 bits of entropy, well above the
    'cryptographically unique' bar. The 'msg_' prefix is the convention
    used by Svix, OpenAI, and the reference Standard Webhooks libraries
    so consumer code that parses message IDs by prefix works without
    Strathon-specific handling.
    """
    return "msg_" + _stdlib_secrets.token_urlsafe(16)


def _send_dramatiq_message(delivery_id: str) -> None:
    """Push the actor invocation onto the queue. Best-effort.

    Failures here are logged but do not propagate: the durable row is
    already committed. The sweeper actor will reclaim orphans on a
    later tick. This is the failure-tolerance the whole architecture
    is designed around — durability lives in Postgres, not in Redis.
    """
    try:
        # Import inside the function to avoid pulling httpx and the
        # broker at receiver startup time.
        from webhooks.actor import deliver_webhook
        deliver_webhook.send(delivery_id)
    except Exception:
        logger.exception(
            "Failed to send Dramatiq message for delivery %s; "
            "sweeper will reclaim", delivery_id,
        )


async def enqueue_delivery(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    policy_id: uuid.UUID,
    url: str,
    payload: dict[str, Any],
) -> WebhookDelivery:
    """Create a webhook_deliveries row and schedule a post-commit send.

    The row is added to the caller's session — committed when the caller
    commits. After commit, an event hook automatically sends the
    Dramatiq message. The caller does not need to do anything else.

    Returns the persisted WebhookDelivery so callers can inspect the
    delivery id (e.g., for response payloads or logging). The id is
    populated by session.flush() inside this function.
    """
    if not url:
        raise ValueError("webhook delivery requires a non-empty url")

    delivery = WebhookDelivery(
        project_id=project_id,
        policy_id=policy_id,
        webhook_id=_generate_webhook_id(),
        url=url,
        payload=payload,
        status="pending",
        attempts=0,
    )
    session.add(delivery)
    # Flush so server-default columns (id, max_attempts, timestamps) are
    # populated and we have a stable id string to enqueue post-commit.
    await session.flush()
    delivery_id_str = str(delivery.id)

    # Emit dispatched metric — counted at row insert regardless of
    # whether the delivery later succeeds. Best-effort: a metrics
    # failure must never break the dispatch.
    try:
        from metrics import get_global_metrics
        m = get_global_metrics()
        if m is not None:
            m.webhook_dispatched.inc()
    except Exception:  # pragma: no cover
        pass

    # SQLAlchemy's session event API does not have an async after_commit;
    # the after_commit hook fires on the underlying sync session via the
    # async session's sync_session attribute. The listener cannot remove
    # itself from inside the callback — SQLAlchemy is iterating its
    # listener deque at that moment, and mutating mid-iteration raises
    # RuntimeError. Instead we use a once-only flag inside the closure
    # so a subsequent commit on the same session is a no-op for this row.
    sync_session = session.sync_session
    _already_sent = []  # list-of-bool, used as a mutable cell

    def _on_commit(_sync_sess):
        if _already_sent:
            return
        _already_sent.append(True)
        _send_dramatiq_message(delivery_id_str)

    event.listen(sync_session, "after_commit", _on_commit)
    return delivery


__all__ = ["enqueue_delivery"]

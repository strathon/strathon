"""Persistence operations for webhook_deliveries.

Three layers of access:

1. Operator-facing list + get: an operator wants to see "did this
   alert fire, and if not, why?" Returns the durable row with its
   attempts, last_response_status, last_error, and current status.

2. Operator-facing replay: when a delivery sits in `dlq` or
   `abandoned` and the operator wants to retry it (e.g. the consumer
   was down for longer than the 24h retry window, or someone fixed a
   bad URL), they POST replay and the row is reset and re-enqueued.

3. Sweeper-facing: find orphaned `pending` rows whose Dramatiq message
   never landed (Redis was unreachable during enqueue, the receiver
   crashed between row insert and message send, etc.). The sweeper
   re-enqueues these on its periodic tick. The threshold is "older
   than this many seconds since creation" — a hard bound on how long
   we trust the queue to be in flight before we re-dispatch.

Cursor pagination
=================

Following GitHub's pattern (per_page + opaque cursor), we paginate by
the (created_at, id) compound key. The cursor is the base64-encoded
JSON of {"ts": <iso8601>, "id": <uuid>}. Cursor-based pagination is
the right call here because the list is append-mostly (new deliveries
land at the head) and offset-based pagination on an append-heavy table
gets confusing for the operator: page 2 of an offset-paginated list
shifts every time a new delivery arrives.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.webhooks import WebhookDelivery

logger = logging.getLogger("strathon.receiver.repositories.webhook_deliveries")


# ---- DTOs ---------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryRow:
    """The shape returned to operators by list + get.

    Notably absent: the signed payload is NOT returned in list responses
    (it can be large and operators usually want the metadata first).
    The single-get endpoint does include payload so operators can
    inspect the body that was sent.
    """
    id: uuid.UUID
    project_id: uuid.UUID
    policy_id: uuid.UUID
    webhook_id: str
    url: str
    status: str
    attempts: int
    max_attempts: int
    last_response_status: int | None
    last_error: str | None
    next_attempt_at: datetime | None
    last_attempt_at: datetime | None
    created_at: datetime

    def to_summary_json(self) -> dict[str, Any]:
        """List-row representation — no payload to keep responses small."""
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "policy_id": str(self.policy_id),
            "webhook_id": self.webhook_id,
            "url": self.url,
            "status": self.status,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "last_response_status": self.last_response_status,
            "last_error": self.last_error,
            "next_attempt_at": (
                self.next_attempt_at.isoformat() if self.next_attempt_at else None
            ),
            "last_attempt_at": (
                self.last_attempt_at.isoformat() if self.last_attempt_at else None
            ),
            "created_at": self.created_at.isoformat(),
        }


def _row_to_dto(row: WebhookDelivery) -> DeliveryRow:
    return DeliveryRow(
        id=row.id,
        project_id=row.project_id,
        policy_id=row.policy_id,
        webhook_id=row.webhook_id,
        url=row.url,
        status=row.status,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        last_response_status=row.last_response_status,
        last_error=row.last_error,
        next_attempt_at=row.next_attempt_at,
        last_attempt_at=row.last_attempt_at,
        created_at=row.created_at,
    )


def _full_json(row: WebhookDelivery) -> dict[str, Any]:
    """Single-get representation: includes payload."""
    base = _row_to_dto(row).to_summary_json()
    base["payload"] = row.payload
    return base


# ---- Cursor encoding ----------------------------------------------------
#
# Opaque base64-JSON cursor of (created_at, id). We deliberately make it
# opaque so we can change the encoding (or the sort key) later without
# breaking clients that round-trip the cursor.


def _encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    data = {"ts": created_at.isoformat(), "id": str(row_id)}
    return base64.urlsafe_b64encode(json.dumps(data).encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        data = json.loads(raw)
        return datetime.fromisoformat(data["ts"]), uuid.UUID(data["id"])
    except Exception as exc:  # noqa: BLE001 - opaque cursor: any decode error is the same
        raise ValueError(f"invalid cursor: {exc}") from exc


# ---- list_deliveries ----------------------------------------------------


VALID_STATUSES = {"pending", "succeeded", "failed_retrying", "dlq", "abandoned"}


async def list_deliveries(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    status: str | None = None,
    policy_id: uuid.UUID | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[DeliveryRow], str | None]:
    """List webhook deliveries for the project, newest first.

    Returns (rows, next_cursor). next_cursor is None when this is the
    last page. The caller is the API handler which serializes the
    summary form (no payload) and appends next_cursor for clients to
    pass back on subsequent requests.

    Filtering by status is the most operationally useful filter —
    "show me the failures" is the typical incident query. policy_id
    narrows further when the operator suspects one specific policy.

    limit defaults to 50, hard cap 200 to keep response sizes bounded.
    Above 200 the cap is silently enforced; we don't 400 since the
    caller's intent (give me as many as you can) is clear.
    """
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200

    if status is not None and status not in VALID_STATUSES:
        raise ValueError(
            f"unknown status: {status!r}. Valid: {sorted(VALID_STATUSES)}"
        )

    stmt = select(WebhookDelivery).where(WebhookDelivery.project_id == project_id)
    if status is not None:
        stmt = stmt.where(WebhookDelivery.status == status)
    if policy_id is not None:
        stmt = stmt.where(WebhookDelivery.policy_id == policy_id)

    if cursor is not None:
        cursor_ts, cursor_id = _decode_cursor(cursor)
        # Strictly-after the cursor row in newest-first order: either
        # an older row, or the same created_at but a smaller id (the
        # tiebreaker that makes the order total).
        stmt = stmt.where(
            or_(
                WebhookDelivery.created_at < cursor_ts,
                and_(
                    WebhookDelivery.created_at == cursor_ts,
                    WebhookDelivery.id < cursor_id,
                ),
            )
        )

    # Fetch one extra to detect "is there a next page?" without a count.
    stmt = stmt.order_by(
        WebhookDelivery.created_at.desc(),
        WebhookDelivery.id.desc(),
    ).limit(limit + 1)

    result = await session.scalars(stmt)
    rows_db = result.all()

    has_more = len(rows_db) > limit
    page = rows_db[:limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)
    return [_row_to_dto(r) for r in page], next_cursor


# ---- get_delivery -------------------------------------------------------


async def get_delivery(
    session: AsyncSession,
    delivery_id: uuid.UUID,
    project_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Fetch a single delivery scoped to the project.

    Returns the full JSON including the payload, or None if no such
    delivery exists in this project. Scoping by project_id prevents
    cross-project id leakage — even if an operator from project A
    learns a delivery id from project B, they can't retrieve it via
    their key.
    """
    row = await session.scalar(
        select(WebhookDelivery).where(
            WebhookDelivery.id == delivery_id,
            WebhookDelivery.project_id == project_id,
        )
    )
    return _full_json(row) if row else None


# ---- replay -------------------------------------------------------------


# Statuses where a replay is meaningful. Replaying a `pending` or
# `failed_retrying` row would race with the retry middleware that's
# already going to fire; replaying `succeeded` is the operator asking
# for a duplicate (they may have edited a downstream system and want
# the alert to fire again). For v1 we allow replay only on terminal
# failure states; succeeded-replay is a future ticket.
REPLAYABLE_STATUSES = {"dlq", "abandoned"}


async def replay_delivery(
    session: AsyncSession,
    delivery_id: uuid.UUID,
    project_id: uuid.UUID,
) -> DeliveryRow | None:
    """Reset a terminal delivery to ``pending`` and reset attempt count.

    Returns the updated row, or None if no such delivery exists for the
    project. Raises ValueError if the delivery exists but is not in a
    replayable state (e.g. it's already pending or already succeeded).

    The caller is expected to dispatch a Dramatiq message after the
    transaction commits — the same after-commit pattern as
    ``webhooks.dispatch.enqueue_delivery``. Without that dispatch the
    row sits pending until the sweeper picks it up, which is at most
    one sweeper tick later (a few minutes). Either path produces the
    same eventual outcome.
    """
    row = await session.scalar(
        select(WebhookDelivery).where(
            WebhookDelivery.id == delivery_id,
            WebhookDelivery.project_id == project_id,
        )
    )
    if row is None:
        return None
    if row.status not in REPLAYABLE_STATUSES:
        raise ValueError(
            f"delivery {delivery_id} is in status {row.status!r}; "
            f"replay is only allowed for {sorted(REPLAYABLE_STATUSES)}"
        )

    now = datetime.now(timezone.utc)
    await session.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == delivery_id)
        .values(
            status="pending",
            attempts=0,
            next_attempt_at=now,
            last_error=None,
            last_response_status=None,
        )
    )
    await session.flush()
    refreshed = await session.scalar(
        select(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
    )
    logger.info(
        "Replayed webhook delivery %s for project %s (was %s)",
        delivery_id, project_id, row.status,
    )
    return _row_to_dto(refreshed)


# ---- sweeper ------------------------------------------------------------


async def find_orphan_pending_deliveries(
    session: AsyncSession,
    *,
    threshold_seconds: int,
    limit: int = 100,
) -> list[uuid.UUID]:
    """Return IDs of pending deliveries that should be re-dispatched.

    A row is orphaned when:
      * status == 'pending'
      * next_attempt_at is older than NOW() - threshold_seconds

    The threshold is the bound on how long we trust the Dramatiq
    message to be in flight. A small threshold sweeps aggressively
    (could double-dispatch a message that's still legitimately in
    the queue); a large threshold means a longer outage window for
    orphan recovery. The default in config.py is 300s (5 minutes).

    Double-dispatch is safe — the actor's first action is a SELECT on
    the row and a fast-return if the status has changed under it; see
    actor.py:_send_one for the idempotency contract.

    The limit caps each sweep so we don't blow up the receiver on a
    catastrophic backlog (e.g. Redis was down for hours and 10k rows
    are pending). The loop runs again on the next interval to drain
    further.
    """
    threshold = datetime.now(timezone.utc) - timedelta(seconds=threshold_seconds)
    stmt = (
        select(WebhookDelivery.id)
        .where(
            WebhookDelivery.status == "pending",
            WebhookDelivery.next_attempt_at < threshold,
        )
        .order_by(WebhookDelivery.next_attempt_at)
        .limit(limit)
    )
    result = await session.scalars(stmt)
    return list(result.all())


__all__ = [
    "DeliveryRow",
    "REPLAYABLE_STATUSES",
    "VALID_STATUSES",
    "find_orphan_pending_deliveries",
    "get_delivery",
    "list_deliveries",
    "replay_delivery",
]

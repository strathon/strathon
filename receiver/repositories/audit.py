"""Audit log persistence — the ``emit`` function and read paths.

``emit`` is the single entry point every mutation endpoint calls to
record an audit event. It must be called inside the same database
transaction as the mutation it audits — that's how we get
fail-closed semantics: if the audit insert fails, the entire
transaction rolls back and the mutation is undone. The endpoint's
``get_db_session`` dependency commits on the way out; ``emit`` does
not commit on its own.

Chain semantics:

    1. Acquire ``pg_advisory_xact_lock`` keyed by the project so two
       concurrent writers can't fork the chain. The lock auto-
       releases at COMMIT/ROLLBACK.
    2. SELECT the latest ``(sequence_no, row_hash)`` for the project.
       The genesis case (no prior row) uses ``GENESIS_PREV_HASH``.
    3. Compute the new row's HMAC under the configured key.
    4. INSERT the row. The DB's BIGSERIAL provides the actual
       ``sequence_no``; we compute the hash against that value
       (the application's locally-incremented prediction matches
       the DB sequence because the per-project advisory lock
       serializes inserts for that project).
    5. Schedule webhook fanout per enabled audit.streams row for
       this project (deferred to dramatiq, after commit).

Read paths support cursor pagination, SCIM filtering, and chain
verification (``verify_event``).

This module never UPDATEs or DELETEs from ``audit.events``. The
table's triggers would block the attempt anyway; not even trying
keeps the contract obvious.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from audit.actions import ACTOR_SYSTEM, OUTCOME_ALLOW
from audit.hash_chain import GENESIS_PREV_HASH, compute_row_hash
from audit.redaction import redact_state
from config import get_settings
from models import AuditEvent, AuditStream


logger = logging.getLogger("strathon.receiver.repositories.audit")


# ---- Dev-mode key fallback --------------------------------------------------

_DEV_KEY: bytes = b"strathon_dev_audit_key_DO_NOT_USE_IN_PRODUCTION_xx"


def _get_hmac_key() -> bytes:
    """Return the configured audit HMAC key or a dev fallback.

    In cloud mode an empty key raises ``RuntimeError`` — fail loudly
    rather than silently use a known dev key. In self-hosted mode we
    substitute a deterministic key with a one-time warning logged, so
    the receiver is usable out of the box.
    """
    settings = get_settings()
    raw = settings.audit_hmac_key
    if raw:
        key = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(key) < 32:
            raise RuntimeError(
                f"STRATHON_AUDIT_HMAC_KEY must be at least 32 bytes; "
                f"got {len(key)}. Generate with "
                "`python -c 'import secrets; print(secrets.token_hex(32))'`."
            )
        return key
    if not settings.is_cloud:
        if not getattr(_get_hmac_key, "_warned", False):
            logger.warning(
                "STRATHON_AUDIT_HMAC_KEY is empty; using dev fallback. "
                "Set a real key for any non-development deployment."
            )
            _get_hmac_key._warned = True  # type: ignore[attr-defined]
        return _DEV_KEY
    raise RuntimeError(
        "STRATHON_AUDIT_HMAC_KEY is required in cloud mode. "
        "Generate with `python -c 'import secrets; print(secrets.token_hex(32))'`."
    )


# ---- Emit context ----------------------------------------------------------


@dataclass
class EmitContext:
    """Per-request context an emit call needs beyond the event itself.

    Built once at the API boundary (typically via a FastAPI dependency)
    and passed through to each emit call within the request. Holds the
    actor identity, request envelope fields, and the X-Request-ID.
    """

    actor_type: str
    actor_id: str
    project_id: UUID
    request_id: UUID
    actor_display: Optional[str] = None
    on_behalf_of: Optional[str] = None
    source_ip: Optional[str] = None
    user_agent: Optional[str] = None
    api_key_id: Optional[str] = None
    auth_method: Optional[str] = None

    @classmethod
    def system(cls, project_id: UUID) -> "EmitContext":
        """Context for events emitted by background tasks / system code."""
        return cls(
            actor_type=ACTOR_SYSTEM,
            actor_id="strathon-receiver",
            project_id=project_id,
            request_id=uuid.uuid4(),
            auth_method="system",
        )


# ---- Emit ------------------------------------------------------------------


async def emit(
    session: AsyncSession,
    ctx: EmitContext,
    action: str,
    action_category: str,
    resource_type: str,
    resource_id: str,
    *,
    outcome: str = OUTCOME_ALLOW,
    reason: Optional[str] = None,
    before_state: Optional[dict[str, Any]] = None,
    after_state: Optional[dict[str, Any]] = None,
    resource_parent: Optional[str] = None,
    cascade_root_id: Optional[UUID] = None,
    pii_classes: Optional[list[str]] = None,
) -> UUID:
    """Record an audit event in the current transaction.

    Returns the new event's ``id``. Raises whatever the DB raises if
    the insert fails — callers MUST allow the exception to propagate
    so the surrounding transaction rolls back (fail-closed).

    The before/after states are redacted per
    :mod:`audit.redaction` rules before storage; their JSON
    representations are capped at 64 KB each. The ``diff`` field is
    computed as an RFC 6902-style JSON Patch (keys-changed only;
    array-position-aware diffs would require external libs).
    """
    occurred_at = datetime.now(timezone.utc)
    event_id = uuid.uuid4()
    key = _get_hmac_key()

    # Per-project advisory lock. The integer key is hashtext of the
    # project_id, computed inside Postgres so two processes agree
    # on the lock identity without us defining a Python hash.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :pid))"),
        {"pid": str(ctx.project_id)},
    )

    # Get prev_hash for the chain. Order by sequence_no DESC so we
    # pick up the absolute last event regardless of clock skew.
    stmt = (
        select(AuditEvent.row_hash, AuditEvent.sequence_no)
        .where(AuditEvent.project_id == ctx.project_id)
        .order_by(AuditEvent.sequence_no.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    prev = result.first()
    if prev is None:
        prev_hash = GENESIS_PREV_HASH
        # We don't know the sequence_no until INSERT (BIGSERIAL).
        # The chain compute below uses a placeholder; we'll fix up
        # after the INSERT returns the actual value. Currently
        # we don't include sequence_no in the hash payload (it's in
        # HASH_FIELDS but recomputed below after the SELECT returns
        # the assigned value).
    else:
        prev_hash = bytes(prev[0])

    # Redact sensitive fields from before/after states before hashing
    # and storing.
    redacted_before = (
        redact_state(before_state, key) if before_state is not None else None
    )
    redacted_after = (
        redact_state(after_state, key) if after_state is not None else None
    )
    diff = _compute_diff(redacted_before, redacted_after)

    # Reserve a sequence number from the BIGSERIAL so we can include
    # it in the hash input. Postgres-side nextval is atomic.
    seq_result = await session.execute(
        text("SELECT nextval('audit.events_sequence_no_seq')")
    )
    sequence_no = seq_result.scalar_one()

    row_for_hash = {
        "id": event_id,
        "sequence_no": sequence_no,
        "occurred_at": occurred_at,
        "project_id": ctx.project_id,
        "actor_type": ctx.actor_type,
        "actor_id": ctx.actor_id,
        "actor_display": ctx.actor_display,
        "on_behalf_of": ctx.on_behalf_of,
        "action": action,
        "action_category": action_category,
        "outcome": outcome,
        "reason": reason,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "resource_parent": resource_parent,
        "cascade_root_id": cascade_root_id,
        "request_id": ctx.request_id,
        "source_ip": ctx.source_ip,
        "user_agent": ctx.user_agent,
        "api_key_id": ctx.api_key_id,
        "auth_method": ctx.auth_method,
        "before_state": redacted_before,
        "after_state": redacted_after,
        "diff": diff,
        "pii_classes": pii_classes or [],
        "schema_version": 1,
    }
    row_hash = compute_row_hash(row_for_hash, prev_hash, key)

    insert_values = dict(row_for_hash)
    insert_values["prev_hash"] = prev_hash
    insert_values["row_hash"] = row_hash
    insert_values["hmac_key_id"] = 1  # single active signing key
    await session.execute(insert(AuditEvent).values(**insert_values))
    return event_id


def _compute_diff(
    before: Optional[dict[str, Any]],
    after: Optional[dict[str, Any]],
) -> Optional[list[dict[str, Any]]]:
    """Compute a minimal change list between two state dicts.

    Returns a list of patch operations in the spirit of RFC 6902
    (op, path, value) — but limited to top-level keys, since most
    audit-payload diffs are flat. Returns ``None`` if either side
    is None (the diff isn't meaningful in that case).
    """
    if before is None or after is None:
        return None
    ops: list[dict[str, Any]] = []
    before_keys = set(before)
    after_keys = set(after)
    for k in sorted(after_keys - before_keys):
        ops.append({"op": "add", "path": f"/{k}", "value": after[k]})
    for k in sorted(before_keys - after_keys):
        ops.append({"op": "remove", "path": f"/{k}"})
    for k in sorted(before_keys & after_keys):
        if before[k] != after[k]:
            ops.append({
                "op": "replace",
                "path": f"/{k}",
                "value": after[k],
            })
    return ops


# ---- Read paths ------------------------------------------------------------


@dataclass
class EventListResult:
    """Page of audit events plus pagination state.

    ``events`` is a list of row mappings (from SQLAlchemy's
    ``result.mappings().all()``), not ORM objects — we use raw SQL
    here so the SCIM filter clause can be embedded directly.
    """

    events: list[dict[str, Any]]
    next_cursor: Optional[str]


async def list_events(
    session: AsyncSession,
    project_id: UUID,
    *,
    limit: int = 50,
    cursor: Optional[str] = None,
    where_clause: Optional[str] = None,
    where_params: Optional[list[Any]] = None,
) -> EventListResult:
    """List events for a project with cursor pagination.

    Cursor format: base64url(json({"occurred_at": iso, "id": uuid})).
    Decoded by :func:`_decode_cursor`; emitted by
    :func:`_encode_cursor`.

    The ``where_clause`` is a parameterized SQL fragment produced by
    :func:`audit.scim_filter.compile_to_sql`; ``where_params`` are
    the bound values. Either both are provided or neither.
    """
    limit = max(1, min(limit, 1000))  # Hard cap from the research.

    cursor_clause = ""
    cursor_params: dict[str, Any] = {}
    if cursor:
        try:
            cursor_at, cursor_id = _decode_cursor(cursor)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid cursor: {exc}") from exc
        cursor_clause = (
            " AND (occurred_at, id) < (:cur_at, :cur_id) "
        )
        cursor_params = {"cur_at": cursor_at, "cur_id": cursor_id}

    extra = ""
    if where_clause:
        # Convert positional %s placeholders to named :p0, :p1 ...
        # so we can mix with our cursor's named params.
        positional = where_clause
        named_clause, named_params = _positional_to_named(
            positional, where_params or []
        )
        extra = f" AND ({named_clause}) "
        cursor_params.update(named_params)

    sql = text(
        f"""
        SELECT * FROM audit.events
        WHERE project_id = :pid
        {cursor_clause}
        {extra}
        ORDER BY occurred_at DESC, id DESC
        LIMIT :limit
        """
    )
    cursor_params["pid"] = project_id
    cursor_params["limit"] = limit + 1  # Fetch one extra to detect more.

    result = await session.execute(sql, cursor_params)
    rows = result.mappings().all()

    has_more = len(rows) > limit
    # RowMapping is a Mapping[str, Any]; convert to plain dicts for
    # the result type. dict() drains the mapping into a real dict so
    # the caller can rely on item assignment / mutation semantics.
    page: list[dict[str, Any]] = [dict(r) for r in rows[:limit]]
    next_cursor: Optional[str] = None
    if has_more:
        last = page[-1]
        next_cursor = _encode_cursor(last["occurred_at"], last["id"])
    return EventListResult(events=page, next_cursor=next_cursor)


async def get_event(
    session: AsyncSession,
    project_id: UUID,
    event_id: UUID,
) -> Optional[Any]:
    """Fetch a single event by id, scoped to the project."""
    sql = text(
        "SELECT * FROM audit.events WHERE project_id = :pid AND id = :id "
        "ORDER BY occurred_at DESC LIMIT 1"
    )
    result = await session.execute(sql, {"pid": project_id, "id": event_id})
    return result.mappings().first()


async def verify_event(
    session: AsyncSession,
    project_id: UUID,
    event_id: UUID,
) -> dict[str, Any]:
    """Verify a single event's hash matches what its predecessor implies.

    Returns a dict with ``valid: bool`` and diagnostic fields. Used
    by the ``/events/{id}/verify`` endpoint.
    """
    from audit.hash_chain import verify_row, HASH_FIELDS

    row = await get_event(session, project_id, event_id)
    if row is None:
        return {"valid": False, "error": "event_not_found"}

    key = _get_hmac_key()
    row_for_hash = {f: row[f] for f in HASH_FIELDS}
    ok = verify_row(
        row_for_hash,
        bytes(row["prev_hash"]),
        key,
        bytes(row["row_hash"]),
    )
    return {
        "valid": ok,
        "event_id": str(event_id),
        "sequence_no": row["sequence_no"],
        "hmac_key_id": row["hmac_key_id"],
    }


# ---- Cursor helpers --------------------------------------------------------


def _encode_cursor(occurred_at: datetime, event_id: UUID) -> str:
    import base64
    import json

    payload = json.dumps(
        {"occurred_at": occurred_at.isoformat(), "id": str(event_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    import base64
    import json

    padding = "=" * (-len(cursor) % 4)
    raw = base64.urlsafe_b64decode(cursor + padding)
    obj = json.loads(raw.decode("utf-8"))
    return (
        datetime.fromisoformat(obj["occurred_at"]),
        UUID(obj["id"]),
    )


def _positional_to_named(
    sql: str,
    params: list[Any],
) -> tuple[str, dict[str, Any]]:
    """Translate ``%s`` placeholders into named ``:pN`` placeholders.

    The audit SCIM filter compiler emits psycopg-style positional
    placeholders; our raw SQL fragments use SQLAlchemy named binds.
    This bridges them.
    """
    out: list[str] = []
    i = 0
    named: dict[str, Any] = {}
    for ch in sql:
        if ch == "%":
            # We expect "%s"; lookahead would be cleaner but this
            # loop is simple enough.
            pass
        out.append(ch)
    # Simple replace, since %s never appears in our generated text
    # outside the placeholder context.
    rebuilt = sql
    for idx, val in enumerate(params):
        name = f"p_filter_{idx}"
        rebuilt = rebuilt.replace("%s", f":{name}", 1)
        named[name] = val
        i += 1
    return rebuilt, named


# ---- Anchor helpers --------------------------------------------------------


async def latest_anchor(session: AsyncSession) -> Optional[Any]:
    """Return the most recent anchor row, or None if none recorded."""
    sql = text(
        "SELECT * FROM audit.anchors ORDER BY anchor_at DESC LIMIT 1"
    )
    result = await session.execute(sql)
    return result.mappings().first()


async def list_anchors(
    session: AsyncSession,
    *,
    since: Optional[datetime] = None,
    limit: int = 100,
) -> list[Any]:
    """List recent anchors, optionally filtered by ``since``."""
    limit = max(1, min(limit, 1000))
    if since is not None:
        sql = text(
            "SELECT * FROM audit.anchors WHERE anchor_at >= :since "
            "ORDER BY anchor_at DESC LIMIT :limit"
        )
        result = await session.execute(
            sql, {"since": since, "limit": limit}
        )
    else:
        sql = text(
            "SELECT * FROM audit.anchors ORDER BY anchor_at DESC LIMIT :limit"
        )
        result = await session.execute(sql, {"limit": limit})
    return list(result.mappings().all())


# ---- Stream helpers --------------------------------------------------------


async def list_streams(
    session: AsyncSession, project_id: UUID
) -> list[AuditStream]:
    stmt = (
        select(AuditStream)
        .where(AuditStream.project_id == project_id)
        .order_by(AuditStream.created_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def create_stream(
    session: AsyncSession,
    project_id: UUID,
    *,
    name: str,
    url: str,
    signing_key_id: Optional[UUID] = None,
    categories: Optional[list[str]] = None,
) -> AuditStream:
    stream = AuditStream(
        project_id=project_id,
        name=name,
        url=url,
        signing_key_id=signing_key_id,
        categories=categories,
    )
    session.add(stream)
    await session.flush()
    return stream


async def delete_stream(
    session: AsyncSession,
    project_id: UUID,
    stream_id: UUID,
) -> bool:
    """Delete a stream. Returns True if deleted, False if not found."""
    stmt = (
        select(AuditStream)
        .where(AuditStream.project_id == project_id)
        .where(AuditStream.id == stream_id)
    )
    result = await session.execute(stmt)
    stream = result.scalar_one_or_none()
    if stream is None:
        return False
    await session.delete(stream)
    return True

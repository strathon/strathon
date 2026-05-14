"""Server-side policy management for the Strathon receiver.

This module handles:
- CRUD for runtime intervention policies (Postgres `policies` table)
- Serving policies to SDK pollers via GET /v1/policies
- Evaluating 'log' and 'alert' actions on inbound spans during ingestion
- Recording matches in the policy_matches table
- Firing alert webhooks asynchronously

'block' and 'steer' actions are NOT evaluated server-side; they are pulled
by the SDK and enforced client-side before the action runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from uuid import UUID

import asyncpg

logger = logging.getLogger("strathon.receiver.policies")

# Vendored expression evaluator: same CEL-based implementation as the SDK.
# Receiver-only copy so we don't depend on the SDK package being installed
# alongside the receiver. Update both when the evaluator changes.

_logger = logging.getLogger("strathon.receiver.policy_eval")


class PolicyExpressionError(ValueError):
    """Raised when a CEL expression fails to parse or compile."""


_COMPILE_CACHE: Dict[str, Any] = {}
_COMPILE_LOCK = threading.Lock()
_ENV: Optional[Any] = None


def _get_env():
    global _ENV
    if _ENV is None:
        import celpy
        _ENV = celpy.Environment()
    return _ENV


def _compile_cached(expression: str):
    if not isinstance(expression, str) or not expression.strip():
        raise PolicyExpressionError("expression must be a non-empty string")
    with _COMPILE_LOCK:
        cached = _COMPILE_CACHE.get(expression)
        if cached is not None:
            return cached
    env = _get_env()
    try:
        ast = env.compile(expression)
        program = env.program(ast)
    except Exception as exc:
        raise PolicyExpressionError(f"failed to compile CEL expression: {exc}") from exc
    with _COMPILE_LOCK:
        _COMPILE_CACHE[expression] = program
    return program


def _evaluate(expression: Optional[str], span_context: Dict[str, Any]) -> bool:
    if not expression:
        return False
    try:
        program = _compile_cached(expression)
    except PolicyExpressionError as exc:
        _logger.warning("invalid policy expression %r: %s", expression, exc)
        return False
    try:
        import celpy
        activation = {
            "name": celpy.celtypes.StringType(span_context.get("name") or ""),
            "attrs": celpy.json_to_cel(span_context.get("attrs") or {}),
        }
        result = program.evaluate(activation)
    except Exception:
        _logger.exception("CEL evaluation crashed for %r", expression)
        return False
    return bool(result)


def _validate(expression: Any) -> None:
    if not isinstance(expression, str):
        raise PolicyExpressionError(
            f"expression must be a string, got {type(expression).__name__}"
        )
    if not expression.strip():
        raise PolicyExpressionError("expression must not be empty")
    _compile_cached(expression)


# ============================================================
# Policy CRUD and ingest-time evaluation start here
# ============================================================


VALID_ACTIONS = {"log", "alert", "block", "steer"}


def _serialize_policy_row(row: asyncpg.Record) -> Dict[str, Any]:
    """Turn a Postgres row into the JSON shape SDKs expect."""
    action_config = row["action_config"]
    if isinstance(action_config, str):
        action_config = json.loads(action_config)
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "name": row["name"],
        "description": row["description"],
        "match_expression": row["match_expression"],  # CEL string
        "action": row["action"],
        "action_config": action_config or {},
        "applies_to": list(row["applies_to"] or []),
        "enabled": row["enabled"],
        "priority": row["priority"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


# ---- CRUD ----


async def list_policies(
    pool: asyncpg.Pool,
    project_id: UUID,
    only_enabled: bool = False,
) -> List[Dict[str, Any]]:
    query = """
        SELECT id, project_id, name, description, match_expression, action,
               action_config, applies_to, enabled, priority, created_at, updated_at
        FROM policies
        WHERE project_id = $1
    """
    params: List[Any] = [project_id]
    if only_enabled:
        query += " AND enabled = TRUE"
    query += " ORDER BY priority DESC, name ASC"

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [_serialize_policy_row(r) for r in rows]


async def create_policy(
    pool: asyncpg.Pool,
    project_id: UUID,
    name: str,
    match_expression: str,
    action: str,
    description: Optional[str] = None,
    action_config: Optional[Dict[str, Any]] = None,
    applies_to: Optional[List[str]] = None,
    enabled: bool = True,
    priority: int = 0,
) -> Dict[str, Any]:
    if action not in VALID_ACTIONS:
        raise ValueError(
            f"action must be one of {sorted(VALID_ACTIONS)}, got {action!r}"
        )
    _validate(match_expression)  # raises PolicyExpressionError if malformed

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO policies (
                project_id, name, description, match_expression, action,
                action_config, applies_to, enabled, priority
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
            RETURNING id, project_id, name, description, match_expression, action,
                      action_config, applies_to, enabled, priority,
                      created_at, updated_at
            """,
            project_id,
            name,
            description,
            match_expression,
            action,
            json.dumps(action_config or {}),
            list(applies_to or []),
            enabled,
            priority,
        )
    return _serialize_policy_row(row)


async def get_policy(
    pool: asyncpg.Pool, project_id: UUID, policy_id: UUID
) -> Optional[Dict[str, Any]]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, project_id, name, description, match_expression, action,
                   action_config, applies_to, enabled, priority, created_at, updated_at
            FROM policies
            WHERE project_id = $1 AND id = $2
            """,
            project_id,
            policy_id,
        )
    return _serialize_policy_row(row) if row else None


async def update_policy(
    pool: asyncpg.Pool,
    project_id: UUID,
    policy_id: UUID,
    **changes: Any,
) -> Optional[Dict[str, Any]]:
    """Apply partial updates. Unknown keys are ignored."""
    allowed = {
        "name",
        "description",
        "match_expression",
        "action",
        "action_config",
        "applies_to",
        "enabled",
        "priority",
    }
    updates = {k: v for k, v in changes.items() if k in allowed and v is not None}
    if not updates:
        return await get_policy(pool, project_id, policy_id)

    if "action" in updates and updates["action"] not in VALID_ACTIONS:
        raise ValueError(
            f"action must be one of {sorted(VALID_ACTIONS)}, got {updates['action']!r}"
        )
    if "match_expression" in updates:
        _validate(updates["match_expression"])

    fragments = []
    params: List[Any] = []
    for i, (k, v) in enumerate(updates.items(), start=1):
        if k == "action_config":
            fragments.append(f"{k} = ${i}::jsonb")
            params.append(json.dumps(v))
        else:
            fragments.append(f"{k} = ${i}")
            params.append(v)
    params.append(project_id)
    params.append(policy_id)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE policies
            SET {', '.join(fragments)}
            WHERE project_id = ${len(params) - 1} AND id = ${len(params)}
            RETURNING id, project_id, name, description, match_expression, action,
                      action_config, applies_to, enabled, priority,
                      created_at, updated_at
            """,
            *params,
        )
    return _serialize_policy_row(row) if row else None


async def delete_policy(
    pool: asyncpg.Pool, project_id: UUID, policy_id: UUID
) -> bool:
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM policies WHERE project_id = $1 AND id = $2",
            project_id,
            policy_id,
        )
    # asyncpg returns 'DELETE n'
    try:
        return int(result.split(" ", 1)[1]) > 0
    except (IndexError, ValueError):
        return False


# ---- Ingest-time evaluation ----


def _build_span_context(span_name: str, attrs: Dict[str, Any]) -> Dict[str, Any]:
    """Match the shape the expression evaluator expects."""
    return {"name": span_name, "attrs": attrs}


def _span_matches_applies_to(span_name: str, applies_to: List[str]) -> bool:
    if not applies_to:
        return True
    return any(token in span_name for token in applies_to)


def evaluate_for_span(
    policies: List[Dict[str, Any]],
    span_name: str,
    attrs: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return the subset of policies that match this span.

    Pure function: no DB, no webhook. Caller decides what to do with matches.
    """
    if not policies:
        return []
    span_ctx = _build_span_context(span_name, attrs)
    matched: List[Dict[str, Any]] = []
    for policy in policies:
        if not policy.get("enabled", True):
            continue
        if not _span_matches_applies_to(span_name, policy.get("applies_to") or []):
            continue
        try:
            if _evaluate(policy["match_expression"], span_ctx):
                matched.append(policy)
        except Exception:
            logger.exception(
                "policy evaluation crashed for policy %s", policy.get("id")
            )
    return matched


async def record_match(
    pool: asyncpg.Pool,
    policy_id: UUID,
    project_id: UUID,
    trace_id: bytes,
    span_id: bytes,
    action: str,
    action_outcome: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Append to policy_matches for audit."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO policy_matches (
                    policy_id, project_id, trace_id, span_id, action,
                    action_outcome, metadata
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                """,
                policy_id,
                project_id,
                trace_id,
                span_id,
                action,
                action_outcome,
                json.dumps(metadata or {}),
            )
    except Exception:
        logger.exception("failed to record policy match for policy %s", policy_id)


async def fire_webhook(url: str, payload: Dict[str, Any], timeout_sec: float = 5.0) -> bool:
    """Fire-and-(mostly)-forget webhook POST.

    Returns True on 2xx, False otherwise. Uses asyncio + aiohttp if available,
    falls back to a thread-pooled requests call. For v0 we keep this tiny and
    use the stdlib so we don't add another dependency.
    """
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        logger.warning("webhook url has unsupported scheme: %s", parsed.scheme)
        return False

    # Use urllib in a thread to avoid blocking the event loop.
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _post_webhook, url, payload, timeout_sec)


def _post_webhook(url: str, payload: Dict[str, Any], timeout_sec: float) -> bool:
    """Synchronous helper run inside a thread executor."""
    import urllib.error
    import urllib.request

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Strathon-Receiver/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        logger.warning("webhook %s returned %s", url, exc.code)
        return False
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.warning("webhook %s failed: %s", url, exc)
        return False
    except Exception:
        logger.exception("webhook %s raised unexpectedly", url)
        return False


__all__ = [
    "VALID_ACTIONS",
    "PolicyExpressionError",
    "create_policy",
    "delete_policy",
    "evaluate_for_span",
    "fire_webhook",
    "get_policy",
    "list_policies",
    "record_match",
    "update_policy",
]

"""CEL expression evaluator for policy match expressions.

Pure module — no DB, no async, no FastAPI. The receiver's ingest hot path
and CRUD validation both call into this module. It's a vendored copy of
the same evaluator the SDK uses; we deliberately don't import from the
SDK package because the receiver must run without the SDK installed
alongside it.

Why CEL: Common Expression Language is non-Turing-complete, side-effect
free, and guaranteed to terminate. It's the same expression language
Kubernetes, Envoy, and gcloud IAM use for policy rules. Users get a
familiar, safe surface; operators get a predictable, bounded evaluator
that can't run arbitrary code.

### Expression context

Every CEL expression is evaluated against a span context that looks like:

    {
        "name":  "langgraph.tool.send_email",
        "attrs": {"gen_ai.tool.name": "send_email", ...},
    }

Additionally, ``now`` is bound to a CEL timestamp of the current UTC
time at the moment of evaluation. Time-based rules use the standard
CEL timestamp methods — ``now.getDayOfWeek()``, ``now.getHours()``,
``now.getDate()`` — and timestamp/duration arithmetic. This is the
cel-spec idiom; the SDK binds the same variable so server-side and
client-side evaluation see the same surface.

Public surface:
    PolicyExpressionError    -- raised on compile failure
    evaluate(expr, ctx)      -- evaluate to bool, swallows runtime errors
    validate_expression(s)   -- raises if not a compilable CEL string

The two leading-underscore helpers (_compile_cached, _get_env) are
exposed for tests but treated as internal.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_logger = logging.getLogger("strathon.receiver.policies_eval")


class PolicyExpressionError(ValueError):
    """Raised when a CEL expression fails to parse or compile."""


_COMPILE_CACHE: Dict[str, Any] = {}
_COMPILE_LOCK = threading.Lock()
_ENV: Optional[Any] = None


def _get_env():
    """Lazy-create the celpy Environment. Single global, thread-safe."""
    global _ENV
    if _ENV is None:
        import celpy
        _ENV = celpy.Environment()
    return _ENV


def _compile_cached(expression: str):
    """Compile a CEL expression once, cache the compiled program.

    Cache key is the expression source string. The compile step is the
    expensive one; once cached, evaluating against a context is cheap.
    """
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


def evaluate(
    expression: Optional[str],
    span_context: Dict[str, Any],
    now: Optional[datetime] = None,
) -> bool:
    """Evaluate a CEL expression against a span context.

    span_context shape:
        {"name": "<span name>", "attrs": {<flat attribute map>}}

    The ``now`` parameter pins the timestamp bound to the CEL ``now``
    variable. Tests pass a deterministic value; production callers omit
    it and get the current UTC time. Naive ``datetime``s are promoted to
    UTC so the policy sees what the caller almost certainly meant — a
    Python-local-time interpretation would break getDayOfWeek/getHours
    in subtle ways.

    Returns False on missing expression, compile error, or runtime error.
    Logs runtime crashes; compile errors are logged at warning level since
    they indicate a user mistake (bad CEL string saved to DB somehow).

    Never raises — the ingest hot path must not crash on a bad policy
    string.
    """
    if not expression:
        return False
    try:
        program = _compile_cached(expression)
    except PolicyExpressionError as exc:
        _logger.warning("invalid policy expression %r: %s", expression, exc)
        return False
    try:
        import celpy
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        activation = {
            "name": celpy.celtypes.StringType(span_context.get("name") or ""),
            "attrs": celpy.json_to_cel(span_context.get("attrs") or {}),
            "now": celpy.celtypes.TimestampType(now),
        }
        result = program.evaluate(activation)
    except Exception:
        _logger.exception("CEL evaluation crashed for %r", expression)
        return False
    return bool(result)


def validate_expression(expression: Any) -> None:
    """Raise PolicyExpressionError if expression isn't a compilable CEL string.

    Used by the policy CRUD layer to reject bad expressions at write time
    rather than discovering them only at evaluation time.
    """
    if not isinstance(expression, str):
        raise PolicyExpressionError(
            f"expression must be a string, got {type(expression).__name__}"
        )
    if not expression.strip():
        raise PolicyExpressionError("expression must not be empty")
    _compile_cached(expression)


__all__ = [
    "PolicyExpressionError",
    "evaluate",
    "validate_expression",
]

"""CEL-based match expression evaluator for Strathon policies.

We use Google's Common Expression Language (CEL), via the pure-Python `celpy`
library. CEL is the same expression language Kubernetes, Envoy, gRPC, and
gcloud IAM use for policy rules. It is:

- Non-Turing-complete, with guaranteed termination
- Side-effect-free
- Safe to evaluate user-supplied expressions
- Recognized by developers across the cloud-native ecosystem

### Expression context

Every CEL expression is evaluated against a span context that looks like:

    {
        "name":  "langgraph.tool.send_email",       # span name
        "attrs": {"gen_ai.tool.name": "send_email", ...},  # flat attributes
    }

Attributes use dot-containing keys (e.g. ``gen_ai.tool.name``) because that's
the OTel GenAI semconv. In CEL you access them with map indexing:

    attrs["gen_ai.tool.name"] == "send_email"

The evaluator additionally binds ``now`` to a CEL ``timestamp`` of the
current UTC time at the moment the expression evaluates. Operators can
write time-based policies using the standard CEL timestamp methods —
``now.getDayOfWeek()``, ``now.getHours()``, ``now.getDate()`` — and
timestamp/duration arithmetic. This is the cel-spec idiom (matches
gcloud IAM, Envoy, KrakenD, etc.), so policies port between systems
without rewriting.

### Example expressions

Block any send_email tool call to a competitor address::

    attrs["gen_ai.tool.name"] == "send_email" &&
    attrs["strathon.tool.args"].contains("@competitor.com")

Alert on expensive LLM calls::

    name.startsWith("langgraph.llm") &&
    attrs["gen_ai.usage.total_tokens"] > 5000

Allow only a specific set of models::

    attrs["gen_ai.request.model"] in ["claude-opus-4-7", "gpt-4o"]

Block weekend tool calls (UTC; Sunday=0, Saturday=6 per cel-spec)::

    now.getDayOfWeek() == 0 || now.getDayOfWeek() == 6

Restrict expensive operations to business hours in a specific timezone::

    name.startsWith("tool.expensive_") &&
    (now.getHours("America/Los_Angeles") < 9 ||
     now.getHours("America/Los_Angeles") >= 17)

This module is dependency-light: it imports `celpy` lazily so importing
`strathon.policy` without using policies costs nothing.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class PolicyExpressionError(ValueError):
    """Raised when a CEL expression fails to parse or compile."""


# Compiled-program cache. Keyed by the raw expression string.
# CEL compilation is fast but caching saves re-parsing on every evaluation.
_COMPILE_CACHE: Dict[str, Any] = {}
_COMPILE_LOCK = threading.Lock()
_ENV: Optional[Any] = None


def _get_env():
    """Lazy-init celpy.Environment so the import is paid once and only when needed."""
    global _ENV
    if _ENV is None:
        try:
            import celpy
        except ImportError as exc:
            raise PolicyExpressionError(
                "celpy is required for policy expression evaluation. "
                "Install with: pip install cel-python"
            ) from exc
        _ENV = celpy.Environment()
    return _ENV


def _compile_cached(expression: str):
    """Compile and cache a CEL expression. Raises PolicyExpressionError on bad input."""
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
        # celpy raises CELParseError or similar; normalize for callers
        raise PolicyExpressionError(f"failed to compile CEL expression: {exc}") from exc

    with _COMPILE_LOCK:
        _COMPILE_CACHE[expression] = program
    return program


def _activation_from_span(
    span_context: Dict[str, Any], now: Optional[datetime] = None,
):
    """Build a CEL activation dict from a span context.

    Converts native Python types into CEL types so the evaluator can index
    into the attrs map and compare strings/numbers correctly. Also binds
    ``now`` to a CEL timestamp; tests can pin the value by passing a
    ``datetime`` (must be timezone-aware), otherwise the current UTC time
    is used.
    """
    import celpy

    name = span_context.get("name") or ""
    attrs = span_context.get("attrs") or {}

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        # CEL timestamps are unambiguously UTC. A naive datetime would
        # be interpreted as local time by Python's strftime/comparison
        # primitives and produce wrong getDayOfWeek/getHours results.
        # Promote to UTC explicitly so the policy sees what the caller
        # almost certainly meant.
        now = now.replace(tzinfo=timezone.utc)

    return {
        "name": celpy.celtypes.StringType(name),
        "attrs": celpy.json_to_cel(attrs),
        "now": celpy.celtypes.TimestampType(now),
    }


def evaluate(
    expression: Optional[str],
    span_context: Dict[str, Any],
    now: Optional[datetime] = None,
) -> bool:
    """Evaluate a CEL expression against a span context.

    Returns True only when the expression evaluates to boolean True. Any
    runtime evaluation error is logged and returns False.

    Important: returning False on error means the policy does NOT match. For a
    block / require_approval policy that means the call is ADMITTED, not denied
    — so this is fail-OPEN at the expression level, not "silent-deny". This is
    acceptable because malformed expressions are rejected at write time by
    validate_expression (a bad expression can't normally reach evaluation), and
    because crashing the evaluator on every span would be worse. If a caller
    needs an eval error on a control-flow policy to fail closed, it must
    special-case the error at the call site (the evaluator only returns a
    bool and does not know the action). Do not read this False as "denied".

    The ``now`` parameter pins the timestamp bound to the CEL ``now``
    variable. Tests pass a deterministic value; production callers omit
    it and get the current UTC time. Either way, the expression sees
    ``now`` as a CEL timestamp and can call the standard timestamp
    methods on it.
    """
    if not expression:
        return False

    try:
        program = _compile_cached(expression)
    except PolicyExpressionError as exc:
        logger.warning("invalid policy expression %r: %s", expression, exc)
        return False

    try:
        activation = _activation_from_span(span_context, now=now)
        result = program.evaluate(activation)
    except Exception:
        logger.exception("CEL evaluation crashed for %r", expression)
        return False

    return bool(result)


def validate(expression: Any) -> None:
    """Compile-only validation. Use this when accepting policies from users.

    Raises PolicyExpressionError on any problem. Does not require a span
    context — it only checks parse-ability + type-ability.
    """
    if not isinstance(expression, str):
        raise PolicyExpressionError(
            f"expression must be a string, got {type(expression).__name__}"
        )
    if not expression.strip():
        raise PolicyExpressionError("expression must not be empty")
    # Compiling raises PolicyExpressionError on failure
    _compile_cached(expression)


def clear_cache() -> None:
    """Drop the compiled-program cache. Useful in tests."""
    with _COMPILE_LOCK:
        _COMPILE_CACHE.clear()

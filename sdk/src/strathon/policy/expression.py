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

### Example expressions

Block any send_email tool call to a competitor address::

    attrs["gen_ai.tool.name"] == "send_email" &&
    attrs["strathon.tool.args"].contains("@competitor.com")

Alert on expensive LLM calls::

    name.startsWith("langgraph.llm") &&
    attrs["gen_ai.usage.total_tokens"] > 5000

Allow only a specific set of models::

    attrs["gen_ai.request.model"] in ["claude-opus-4-7", "gpt-4o"]

This module is dependency-light: it imports `celpy` lazily so importing
`strathon.policy` without using policies costs nothing.
"""

from __future__ import annotations

import logging
import threading
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


def _activation_from_span(span_context: Dict[str, Any]):
    """Build a CEL activation dict from a span context.

    Converts native Python types into CEL types so the evaluator can index
    into the attrs map and compare strings/numbers correctly.
    """
    import celpy

    name = span_context.get("name") or ""
    attrs = span_context.get("attrs") or {}

    return {
        "name": celpy.celtypes.StringType(name),
        "attrs": celpy.json_to_cel(attrs),
    }


def evaluate(expression: Optional[str], span_context: Dict[str, Any]) -> bool:
    """Evaluate a CEL expression against a span context.

    Returns True only when the expression evaluates to boolean True. Any
    runtime evaluation error is logged and returns False — we prefer
    silent-deny over crashing the receiver / SDK.
    """
    if not expression:
        return False

    try:
        program = _compile_cached(expression)
    except PolicyExpressionError as exc:
        logger.warning("invalid policy expression %r: %s", expression, exc)
        return False

    try:
        activation = _activation_from_span(span_context)
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

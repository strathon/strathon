"""Ingest-side policy composition.

This module is intentionally thin:

  - CRUD lives in receiver/repositories/policies.py (uses AsyncSession)
  - CEL expression machinery lives in receiver/policies_eval.py (pure)
  - This module owns the ingest hot path: matching a span against the
    set of policies, and firing alert webhooks. Those two things are
    pure-Python composition, no DB.

We deliberately re-export `PolicyExpressionError` from policies_eval so
existing import sites in main.py (`from policies import PolicyExpressionError`)
keep working without churn.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

# Re-export so back-compat imports from main.py and elsewhere keep working
from policies_eval import PolicyExpressionError  # noqa: F401
from policies_eval import evaluate as _evaluate

logger = logging.getLogger("strathon.receiver.policies")


# Re-exported for back-compat; the canonical source is schemas/policies.py.
# Kept in sync with it so a caller importing from here cannot get a narrower
# (stale) set that would wrongly reject throttle / require_approval policies.
VALID_ACTIONS = {"log", "alert", "block", "steer", "throttle", "allow", "require_approval"}


# ---- Ingest-time evaluation (pure) --------------------------------------


def _build_span_context(span_name: str, attrs: Dict[str, Any]) -> Dict[str, Any]:
    """Match the shape the CEL evaluator expects."""
    return {"name": span_name, "attrs": attrs}


def _span_matches_applies_to(span_name: str, applies_to: List[str]) -> bool:
    """Empty applies_to means 'every span'; otherwise dot-segment-path match.

    Each token in applies_to is matched against the span name as a whole
    sequence of dot-separated segments. ``"tool"`` matches
    ``"langgraph.tool.send_email"`` (because ``tool`` is one of the
    segments) but does NOT match ``"pool.X"`` (no segment equals
    ``"tool"``). Multi-segment tokens are also supported:
    ``"langgraph.tool"`` matches ``"langgraph.tool.send_email"`` as a
    prefix-aligned multi-segment path.

    The SDK enforcer (``sdk/src/strathon/policy/enforcer.py``) carries
    the same logic so server-side ingest filtering and SDK-side
    pre-call filtering agree by construction.
    """
    if not applies_to:
        return True
    if not span_name:
        return False
    return any(_segment_path_match(span_name, token) for token in applies_to)


def _segment_path_match(name: str, token: str) -> bool:
    """True iff ``token`` aligns with whole dot-separated segments of ``name``.

    Mirror of the SDK helper of the same name. Kept duplicated rather
    than imported across the SDK/receiver boundary so the receiver has
    no compile-time dependency on the SDK package.
    """
    if not token:
        return False
    if name == token:
        return True
    return (
        name.startswith(token + ".")
        or name.endswith("." + token)
        or ("." + token + ".") in name
    )


class PolicyEvaluationUnavailable(RuntimeError):
    """Every candidate policy failed to evaluate.

    An empty result from ``evaluate_for_span`` used to mean two different
    things: no policy matched, or evaluation could not run at all. On a
    recording path those are equivalent. On an enforcement surface they are
    the difference between allowing a call and blocking it, and the caller
    had no way to tell them apart -- a missing CEL engine made every policy
    raise, every one get skipped, and the empty list read as "nothing
    matched". Raising here makes that case reach a fail-closed handler
    instead of being mistaken for a clean pass.

    Only raised when *every* evaluable policy failed. A single malformed
    expression is still swallowed and logged, so one bad policy cannot stop
    the rest from being evaluated.
    """


def evaluate_for_span(
    policies: List[Dict[str, Any]],
    span_name: str,
    attrs: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return the subset of policies that match this span.

    Pure function: no DB, no webhook. Caller decides what to do with matches.
    Skips disabled policies and policies whose applies_to filter excludes
    this span. A crash inside an individual policy is swallowed and logged so
    one bad policy can't poison the rest of ingest.

    Raises PolicyEvaluationUnavailable if every policy that was actually
    evaluated crashed, which means evaluation is broken rather than simply
    unmatched. Callers that enforce must let that propagate to their
    fail-closed path; callers that only record may catch it and carry on.
    """
    if not policies:
        return []
    span_ctx = _build_span_context(span_name, attrs)
    matched: List[Dict[str, Any]] = []
    evaluated = 0
    failed = 0
    for policy in policies:
        if not policy.get("enabled", True):
            continue
        if not _span_matches_applies_to(span_name, policy.get("applies_to") or []):
            continue
        evaluated += 1
        try:
            if _evaluate(policy["match_expression"], span_ctx):
                matched.append(policy)
        except Exception:
            failed += 1
            logger.exception(
                "policy evaluation crashed for policy %s", policy.get("id")
            )
    if evaluated and failed == evaluated:
        raise PolicyEvaluationUnavailable(
            f"all {evaluated} candidate policies failed to evaluate for span "
            f"{span_name!r}; treating as evaluation failure, not as no-match"
        )
    return matched


# Webhook firing lives in the webhooks/ package.
# fire_webhook here was fire-and-forget with no retries, signing, or
# durability — see webhooks.dispatch.enqueue_delivery for the
# reliable replacement.


__all__ = [
    "PolicyEvaluationUnavailable",
    "PolicyExpressionError",
    "VALID_ACTIONS",
    "evaluate_for_span",
]

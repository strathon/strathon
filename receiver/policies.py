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
from typing import Any, Dict, List, Optional

# Re-export so back-compat imports from main.py and elsewhere keep working
from policies_eval import PolicyExpressionError  # noqa: F401
from policies_eval import MatchResult, evaluate_tristate

logger = logging.getLogger("strathon.receiver.policies")


# Re-exported for back-compat; the canonical source is schemas/policies.py.
# Kept in sync with it so a caller importing from here cannot get a narrower
# (stale) set that would wrongly reject throttle / require_approval policies.
VALID_ACTIONS = {"log", "alert", "block", "steer", "throttle", "allow", "require_approval"}

# Actions that stop or gate a tool call. If a policy with one of these cannot be
# evaluated, the span must fail closed (an unevaluable block might have been the
# decisive one). steer/log/alert/allow cannot turn an allow into a block, so an
# error on those is logged and skipped rather than failing the span closed.
_ENFORCING_ACTIONS = {"block", "require_approval", "throttle"}


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
    """A policy that had to be evaluated could not be.

    An empty result from ``evaluate_for_span`` used to mean two different
    things: no policy matched, or evaluation could not run at all. On a
    recording path those are equivalent. On an enforcement surface they are
    the difference between allowing a call and blocking it, and the caller
    had no way to tell them apart -- a missing CEL engine made every policy
    raise, every one get skipped, and the empty list read as "nothing
    matched". Raising here makes that case reach a fail-closed handler
    instead of being mistaken for a clean pass.

    Raised when an enforcing policy (block, require_approval, throttle) could
    not be evaluated -- its decision cannot be confirmed, so an enforcement
    surface must fail closed -- or when every evaluable policy failed. An error
    in a non-enforcing policy is still swallowed and logged.

    ``matches`` carries the policies that DID evaluate to a match before the
    failure. Enforcement surfaces ignore it and fail closed; the recording path
    records these so one unevaluable policy does not erase another policy's
    legitimate match on the same span.
    """

    def __init__(self, *args: object, matches: "List[Dict[str, Any]] | None" = None):
        super().__init__(*args)
        self.matches: List[Dict[str, Any]] = matches or []


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

    Raises PolicyEvaluationUnavailable when a policy cannot be evaluated and
    treating it as no-match would be unsafe:
      - a policy whose action ENFORCES (block, require_approval, throttle)
        errors during evaluation -- it might have been the decisive block, so
        the span must fail closed rather than silently allow; or
      - every candidate policy failed to evaluate, so evaluation is broken
        rather than simply unmatched.
    An error on a non-enforcing policy (log, alert, steer, allow) is logged and
    skipped -- it cannot turn an allow into a block, so it must not fail the
    whole span closed. Callers that enforce must let the exception propagate to
    their fail-closed path; callers that only record catch it and carry on.
    """
    if not policies:
        return []
    span_ctx = _build_span_context(span_name, attrs)
    matched: List[Dict[str, Any]] = []
    evaluated = 0
    failed = 0
    enforcing_failure: Optional[Dict[str, Any]] = None
    for policy in policies:
        if not policy.get("enabled", True):
            continue
        if not _span_matches_applies_to(span_name, policy.get("applies_to") or []):
            continue
        evaluated += 1
        result = evaluate_tristate(policy["match_expression"], span_ctx)
        if result is MatchResult.MATCH:
            matched.append(policy)
        elif result is MatchResult.ERROR:
            failed += 1
            logger.warning(
                "policy %s could not be evaluated for span %s (action=%s)",
                policy.get("id"), span_name, policy.get("action"),
            )
            # Record the first enforcing policy that failed, but keep evaluating
            # the rest. The loop must finish so `matched` holds every policy that
            # did match; raising here would drop the matches of policies later in
            # the priority order, which the recording path needs to keep. The
            # decision to fail closed is made after the loop.
            if enforcing_failure is None and policy.get("action") in _ENFORCING_ACTIONS:
                enforcing_failure = policy
        # NO_MATCH: policy did not apply; skip.

    if enforcing_failure is not None:
        # An enforcing policy could not be evaluated: we cannot confirm the call
        # is allowed, so the span must fail closed. Carry every match found so an
        # enforcement surface ignores them and blocks, while the recording path
        # keeps them. This is the case that was previously swallowed to a silent
        # allow.
        raise PolicyEvaluationUnavailable(
            f"enforcing policy {enforcing_failure.get('id')!r} (action "
            f"{enforcing_failure.get('action')!r}) could not be evaluated for span "
            f"{span_name!r}; failing closed rather than treating as no-match",
            matches=matched,
        )
    if evaluated and failed == evaluated:
        raise PolicyEvaluationUnavailable(
            f"all {evaluated} candidate policies failed to evaluate for span "
            f"{span_name!r}; treating as evaluation failure, not as no-match",
            matches=matched,
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

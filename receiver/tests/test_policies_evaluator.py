"""Tests for ``receiver/policies.py::evaluate_for_span``.

This module is the ingest-side equivalent of the SDK's PolicyEnforcer:
when the receiver receives a span, it iterates the project's policies
and returns the subset that match. Until now the only coverage was in
``test_policies_repository.py`` — which proves the field round-trips
through the DB but never proves the evaluator actually filters by it.

These tests close that gap and lock in the dot-segment-path semantic
for applies_to. The SDK enforcer has a mirror of the same logic; see
``sdk/tests/test_policy_enforcer.py`` for the parallel suite.
"""

from __future__ import annotations

import os
import sys

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)


# Import after sys.path setup so the receiver's top-level modules resolve
import pytest  # noqa: E402

import policies  # noqa: E402
from policies import evaluate_for_span  # noqa: E402


def _policy(applies_to: list[str], match_expression: str = "true") -> dict:
    return {
        "id": "00000000-0000-0000-0000-000000000099",
        "name": "test_policy",
        "match_expression": match_expression,
        "action": "alert",
        "action_config": {"webhook_url": "http://localhost:9999/never"},
        "applies_to": applies_to,
        "enabled": True,
        "priority": 0,
    }


# ---- Empty applies_to ----------------------------------------------------


def test_empty_applies_to_matches_every_span():
    matches = evaluate_for_span(
        policies=[_policy(applies_to=[])],
        span_name="anything.at.all",
        attrs={},
    )
    assert len(matches) == 1


def test_no_applies_to_field_matches_every_span():
    """A policy dict missing the applies_to key entirely (treated as [])."""
    p = _policy(applies_to=[])
    del p["applies_to"]
    matches = evaluate_for_span(policies=[p], span_name="x.y", attrs={})
    assert len(matches) == 1


# ---- Segment-path matching ----------------------------------------------


def test_single_segment_token_matches_interior_segment():
    matches = evaluate_for_span(
        policies=[_policy(applies_to=["tool"])],
        span_name="langgraph.tool.send_email",
        attrs={},
    )
    assert len(matches) == 1


def test_single_segment_token_matches_prefix_segment():
    matches = evaluate_for_span(
        policies=[_policy(applies_to=["langgraph"])],
        span_name="langgraph.tool.send_email",
        attrs={},
    )
    assert len(matches) == 1


def test_single_segment_token_matches_suffix_segment():
    matches = evaluate_for_span(
        policies=[_policy(applies_to=["send_email"])],
        span_name="langgraph.tool.send_email",
        attrs={},
    )
    assert len(matches) == 1


def test_multi_segment_token_scopes_to_one_framework():
    """The classic case: 'langgraph.tool' filters to LangGraph tools only."""
    span_lg = "langgraph.tool.send_email"
    span_crew = "crewai.tool.send_email"
    policies = [_policy(applies_to=["langgraph.tool"])]

    assert len(evaluate_for_span(policies, span_name=span_lg,   attrs={})) == 1
    assert len(evaluate_for_span(policies, span_name=span_crew, attrs={})) == 0


# ---- The footgun the old substring rule allowed ------------------------


def test_segment_rule_rejects_substring_within_a_segment():
    """'tool' as a token must NOT match 'pool' — they're different segments.

    Under the previous substring rule this would have matched (because
    'tool' is a substring of 'pool'). The dot-segment-path rule fixes it.
    """
    matches = evaluate_for_span(
        policies=[_policy(applies_to=["tool"])],
        span_name="pool",
        attrs={},
    )
    assert matches == []


def test_segment_rule_rejects_substring_inside_an_unrelated_segment():
    matches = evaluate_for_span(
        policies=[_policy(applies_to=["tool"])],
        span_name="framework.pool.x",
        attrs={},
    )
    assert matches == []


def test_segment_rule_requires_segment_aligned_multi_segment_token():
    """A multi-segment token must align on segment boundaries on both ends.

    'tool.send' is not a segment-aligned slice of 'langgraph.tool.send_email'
    (the second segment is 'send_email', not 'send'), so it must not match.
    """
    matches = evaluate_for_span(
        policies=[_policy(applies_to=["tool.send"])],
        span_name="langgraph.tool.send_email",
        attrs={},
    )
    assert matches == []


# ---- Multi-token OR semantics ------------------------------------------


def test_token_list_is_or():
    policies = [_policy(applies_to=["tool", "llm"])]
    assert len(evaluate_for_span(policies, "langgraph.tool.x", attrs={})) == 1
    assert len(evaluate_for_span(policies, "langgraph.llm.x",  attrs={})) == 1
    assert len(evaluate_for_span(policies, "langgraph.crew.x", attrs={})) == 0


# ---- Interactions with disabled / non-matching expressions -------------


def test_applies_to_matches_but_expression_does_not():
    """applies_to gate passes, but the CEL still has to evaluate true."""
    matches = evaluate_for_span(
        policies=[_policy(applies_to=["tool"], match_expression="false")],
        span_name="langgraph.tool.send_email",
        attrs={},
    )
    assert matches == []


def test_disabled_policy_is_skipped_regardless_of_applies_to():
    p = _policy(applies_to=["tool"])
    p["enabled"] = False
    matches = evaluate_for_span(
        policies=[p],
        span_name="langgraph.tool.send_email",
        attrs={},
    )
    assert matches == []


# ---- Evaluation failure is not a no-match ------------------------------


def test_all_policies_failing_raises_rather_than_returning_empty(monkeypatch):
    """An unevaluable policy set must not look like a clean no-match.

    Returning [] for "every policy crashed" is indistinguishable from
    "nothing matched", and the enforcement surfaces read the second as
    permission to allow. Raising forces them into their fail-closed paths.
    """
    # evaluate_for_span now calls evaluate_tristate; simulate every policy
    # being unevaluable (the systemic-failure case) by returning ERROR for all
    # of them. With all candidates failing, the backstop fails closed.
    from policies_eval import MatchResult

    monkeypatch.setattr(
        policies, "evaluate_tristate", lambda *a, **k: MatchResult.ERROR
    )

    with pytest.raises(policies.PolicyEvaluationUnavailable):
        evaluate_for_span(
            policies=[_policy(applies_to=[]), _policy(applies_to=[])],
            span_name="tool.call",
            attrs={},
        )


def test_one_bad_policy_still_lets_the_rest_evaluate():
    """Resilience is preserved: a single non-enforcing policy that cannot be
    evaluated is skipped, and the rest still evaluate. Only a total failure
    (all candidates) or an enforcing policy's error escalates to fail-closed.

    "BROKEN" is not a compilable CEL string, so evaluate_tristate returns ERROR
    for it. Paired with a clean matching policy, the clean one still matches.
    """
    matched = evaluate_for_span(
        policies=[
            _policy(applies_to=[], match_expression="BROKEN"),  # action=alert
            _policy(applies_to=[], match_expression="true"),
        ],
        span_name="tool.call",
        attrs={},
    )
    assert len(matched) == 1
    assert matched[0]["match_expression"] == "true"


def test_no_policies_returns_empty_without_raising():
    """An empty policy set is a genuine no-match, not a failure."""
    assert evaluate_for_span(policies=[], span_name="tool.call", attrs={}) == []


# ---- fail closed when a policy cannot be evaluated -----------------------
#
# The CEL evaluator used to swallow a runtime error (e.g. a referenced
# attribute being absent) to False, so evaluate_for_span read it as "did not
# match" and the enforcement surfaces silently allowed the call. It now returns
# a distinct ERROR, and evaluate_for_span fails closed when the errored policy
# is an enforcing one.

from policies import PolicyEvaluationUnavailable  # noqa: E402


def _policy_action(action: str, match_expression: str) -> dict:
    p = _policy(applies_to=[], match_expression=match_expression)
    p["action"] = action
    return p


# An expression that raises at runtime: it references an attribute that is not
# present on the span, and CEL surfaces the "no such key" error precisely when
# the preceding conjunct already matched.
_ERRORING_EXPR = (
    'attrs["gen_ai.tool.name"] == "wire_money" && attrs["absent.attr"] == "x"'
)
_SPAN_ATTRS = {"gen_ai.tool.name": "wire_money"}


def test_errored_block_policy_fails_closed():
    """A block policy that cannot be evaluated raises rather than allowing."""
    with pytest.raises(PolicyEvaluationUnavailable):
        evaluate_for_span(
            policies=[_policy_action("block", _ERRORING_EXPR)],
            span_name="tool.wire_money",
            attrs=_SPAN_ATTRS,
        )


def test_errored_require_approval_policy_fails_closed():
    with pytest.raises(PolicyEvaluationUnavailable):
        evaluate_for_span(
            policies=[_policy_action("require_approval", _ERRORING_EXPR)],
            span_name="tool.wire_money",
            attrs=_SPAN_ATTRS,
        )


def test_errored_nonenforcing_policy_does_not_fail_closed():
    """An error on a log/alert/steer policy cannot turn an allow into a block,
    so it is logged and skipped -- as long as some other policy evaluated
    cleanly (otherwise the all-candidates-failed backstop fails closed, which
    is the correct systemic-failure signal). Pair the erroring alert with a
    clean no-match block so evaluation is not wholly broken."""
    clean = _policy_action("block", 'attrs["gen_ai.tool.name"] == "other"')
    erroring_alert = _policy_action("alert", _ERRORING_EXPR)
    matches = evaluate_for_span(
        policies=[clean, erroring_alert],
        span_name="tool.wire_money",
        attrs=_SPAN_ATTRS,
    )
    # clean policy is a no-match, erroring alert is skipped -> no matches, no raise
    assert matches == []


def test_clean_match_still_matches_alongside_error_semantics():
    """A well-formed block policy that genuinely matches still returns."""
    good = _policy_action("block", 'attrs["gen_ai.tool.name"] == "wire_money"')
    matches = evaluate_for_span(
        policies=[good], span_name="tool.wire_money", attrs=_SPAN_ATTRS
    )
    assert len(matches) == 1
    assert matches[0]["action"] == "block"


def test_clean_no_match_is_not_an_error():
    """A policy that evaluates to false is a clean no-match, not fail-closed."""
    nomatch = _policy_action("block", 'attrs["gen_ai.tool.name"] == "other"')
    matches = evaluate_for_span(
        policies=[nomatch], span_name="tool.wire_money", attrs=_SPAN_ATTRS
    )
    assert matches == []


# ---- non-boolean match expressions fail closed, not silent bool() coercion ---

def test_non_boolean_result_is_error_not_silent_match():
    """A match expression that returns a non-bool must be ERROR, not coerced.

    `attrs["gen_ai.tool.name"]` (comparison accidentally omitted) evaluates to
    the tool-name string. bool() of a non-empty string is True, which would
    silently match every call that has the attribute -- a broken policy behaving
    invisibly. It must fail closed instead.
    """
    from policies_eval import evaluate_tristate, MatchResult

    r = evaluate_tristate(
        'attrs["gen_ai.tool.name"]',
        {"attrs": {"gen_ai.tool.name": "wire_transfer"}},
    )
    assert r is MatchResult.ERROR


def test_non_boolean_falsy_result_is_error_not_silent_no_match():
    """A falsy non-bool (e.g. int 0) must also be ERROR, not a silent no-match."""
    from policies_eval import evaluate_tristate, MatchResult

    r = evaluate_tristate('attrs["n"]', {"attrs": {"n": 0}})
    assert r is MatchResult.ERROR


def test_boolean_results_still_work():
    """Proper boolean expressions are unaffected by the non-bool guard."""
    from policies_eval import evaluate_tristate, MatchResult

    ctx = {"attrs": {"t": "wire_transfer"}}
    assert evaluate_tristate('attrs["t"] == "wire_transfer"', ctx) is MatchResult.MATCH
    assert evaluate_tristate('attrs["t"] == "other"', ctx) is MatchResult.NO_MATCH


def test_erroring_enforcing_policy_preserves_other_matches_on_the_exception():
    """An enforcing policy that cannot be evaluated must fail closed, but it
    must not erase a different policy that matched the same span.

    Regression: a block policy referencing an attribute the span lacks raised
    (correctly, to fail closed on the enforcement path), but the raise threw
    away an alert policy that had already matched -- so the recording path saw
    zero matches for a span that genuinely matched a policy. The exception now
    carries the clean matches for the recording path to keep.
    """
    from policies import PolicyEvaluationUnavailable

    alert = {
        "id": "alert-1", "enabled": True, "applies_to": ["langgraph.tool"],
        "match_expression": "true", "action": "alert",
    }
    # References strathon.tool.args, which this span does not have -> ERROR.
    block = {
        "id": "block-1", "enabled": True, "applies_to": [],
        "match_expression": 'attrs["strathon.tool.args"].contains("x")',
        "action": "block",
    }

    with pytest.raises(PolicyEvaluationUnavailable) as exc_info:
        evaluate_for_span(
            [alert, block], "langgraph.tool.send_email",
            {"gen_ai.tool.name": "send_email"},
        )
    # Enforcement surfaces fail closed on the raise; the recording path reads
    # exc.matches and keeps the alert match.
    matched_ids = [p["id"] for p in exc_info.value.matches]
    assert matched_ids == ["alert-1"]


def test_match_preservation_is_independent_of_policy_order():
    """The erroring enforcing policy can come before the matching one.

    Regression: matches were preserved only when the matching policy was
    evaluated before the enforcing policy that failed. Policies arrive priority
    DESC, so a failing block often comes first; the earlier raise then dropped a
    lower-priority match. The full policy set is now evaluated before failing
    closed, so the exception carries every match regardless of order.
    """
    from policies import PolicyEvaluationUnavailable

    alert = {
        "id": "alert-1", "enabled": True, "applies_to": ["langgraph.tool"],
        "match_expression": "true", "action": "alert",
    }
    block = {
        "id": "block-1", "enabled": True, "applies_to": [],
        "match_expression": 'attrs["strathon.tool.args"].contains("x")',
        "action": "block",
    }

    # Enforcing policy FIRST -- the order that used to lose the match.
    with pytest.raises(PolicyEvaluationUnavailable) as exc_info:
        evaluate_for_span(
            [block, alert], "langgraph.tool.send_email",
            {"gen_ai.tool.name": "send_email"},
        )
    assert [p["id"] for p in exc_info.value.matches] == ["alert-1"]


def test_erroring_nonenforcing_policy_does_not_raise_and_keeps_matches():
    """A non-enforcing policy that errors is skipped, not raised, and other
    matches are returned normally (no exception)."""
    alert = {
        "id": "alert-1", "enabled": True, "applies_to": ["langgraph.tool"],
        "match_expression": "true", "action": "alert",
    }
    bad_log = {
        "id": "log-1", "enabled": True, "applies_to": [],
        "match_expression": 'attrs["missing"].contains("x")', "action": "log",
    }
    matches = evaluate_for_span(
        [alert, bad_log], "langgraph.tool.send_email",
        {"gen_ai.tool.name": "send_email"},
    )
    assert [p["id"] for p in matches] == ["alert-1"]

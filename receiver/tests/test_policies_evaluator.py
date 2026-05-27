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

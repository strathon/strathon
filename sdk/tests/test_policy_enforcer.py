"""Tests for the SDK-side PolicyEnforcer."""

import pytest

from strathon.policy import Policy, StrathonPolicyBlocked
from strathon.policy.enforcer import PolicyEnforcer


def _make_enforcer():
    # endpoint doesn't matter because we'll use set_policies_for_testing
    return PolicyEnforcer(
        endpoint="http://localhost:9999",
        api_key="test",
        project_id="00000000-0000-0000-0000-000000000001",
    )


def _block_policy(name="block_competitor", priority=10):
    return Policy(
        id=f"pol_{name}",
        project_id="00000000-0000-0000-0000-000000000001",
        name=name,
        match_expression=(
            'attrs["gen_ai.tool.name"] == "send_email" && '
            'attrs["strathon.tool.args"].contains("@competitor.com")'
        ),
        action="block",
        action_config={"message": "Cannot email competitors."},
        priority=priority,
    )


def _steer_policy(name="steer_competitor", priority=5):
    return Policy(
        id=f"pol_{name}",
        project_id="00000000-0000-0000-0000-000000000001",
        name=name,
        match_expression='attrs["strathon.tool.args"].contains("competitor")',
        action="steer",
        action_config={"replacement": "Suggest internal alternative instead."},
        priority=priority,
    )


def _log_policy(name="log_anything"):
    return Policy(
        id=f"pol_{name}",
        project_id="00000000-0000-0000-0000-000000000001",
        name=name,
        match_expression='has(attrs["gen_ai.tool.name"])',
        action="log",
    )


def _competitor_email_context():
    return {
        "name": "langgraph.tool.send_email",
        "attrs": {
            "gen_ai.tool.name": "send_email",
            "strathon.tool.args": '{"to": "sales@competitor.com", "body": "hi"}',
        },
    }


def _innocuous_email_context():
    return {
        "name": "langgraph.tool.send_email",
        "attrs": {
            "gen_ai.tool.name": "send_email",
            "strathon.tool.args": '{"to": "team@mycompany.com", "body": "hi"}',
        },
    }


# ---- Decision flow ----


def test_check_policy_returns_allow_with_no_policies():
    enforcer = _make_enforcer()
    decision = enforcer.check_policy(_competitor_email_context())
    assert decision.is_allow


def test_check_policy_blocks_on_match():
    enforcer = _make_enforcer()
    enforcer.set_policies_for_testing([_block_policy()])
    decision = enforcer.check_policy(_competitor_email_context())
    assert decision.is_block
    assert decision.message == "Cannot email competitors."
    assert decision.policy_name == "block_competitor"


def test_check_policy_allows_when_no_match():
    enforcer = _make_enforcer()
    enforcer.set_policies_for_testing([_block_policy()])
    decision = enforcer.check_policy(_innocuous_email_context())
    assert decision.is_allow


def test_check_policy_steers_on_match():
    enforcer = _make_enforcer()
    enforcer.set_policies_for_testing([_steer_policy()])
    decision = enforcer.check_policy(_competitor_email_context())
    assert decision.is_steer
    assert decision.replacement == "Suggest internal alternative instead."


def test_check_policy_ignores_log_and_alert_actions():
    """log/alert never short-circuit client-side; they're server-side concerns."""
    enforcer = _make_enforcer()
    enforcer.set_policies_for_testing([_log_policy()])
    decision = enforcer.check_policy(_competitor_email_context())
    assert decision.is_allow


def test_check_policy_skips_disabled_policies():
    p = _block_policy()
    disabled = Policy(
        id=p.id,
        project_id=p.project_id,
        name=p.name,
        match_expression=p.match_expression,
        action=p.action,
        action_config=p.action_config,
        applies_to=p.applies_to,
        enabled=False,
        priority=p.priority,
    )
    enforcer = _make_enforcer()
    enforcer.set_policies_for_testing([disabled])
    decision = enforcer.check_policy(_competitor_email_context())
    assert decision.is_allow


def test_check_policy_block_wins_over_lower_priority_steer():
    block = _block_policy(priority=100)
    steer = _steer_policy(priority=1)
    enforcer = _make_enforcer()
    enforcer.set_policies_for_testing([steer, block])  # deliberately wrong order
    decision = enforcer.check_policy(_competitor_email_context())
    # Higher priority block should win even though we added steer first
    assert decision.is_block


def test_check_policy_steer_wins_over_lower_priority_block():
    block = _block_policy(priority=1)
    steer = _steer_policy(priority=100)
    enforcer = _make_enforcer()
    enforcer.set_policies_for_testing([block, steer])
    decision = enforcer.check_policy(_competitor_email_context())
    assert decision.is_steer


# ---- applies_to filter ----


def test_applies_to_limits_scope():
    """A policy that applies_to ['tool'] shouldn't fire on an LLM span."""
    p = Policy(
        id="pol_tool_only",
        project_id="00000000-0000-0000-0000-000000000001",
        name="tool_only",
        match_expression='name != ""',  # always matches when name is set
        action="block",
        applies_to=["tool"],
    )
    enforcer = _make_enforcer()
    enforcer.set_policies_for_testing([p])

    tool_ctx = {"name": "langgraph.tool.send_email", "attrs": {}}
    llm_ctx = {"name": "langgraph.llm", "attrs": {}}

    assert enforcer.check_policy(tool_ctx).is_block
    assert enforcer.check_policy(llm_ctx).is_allow


def test_empty_applies_to_means_all_spans():
    p = _block_policy()
    assert p.applies_to == []
    enforcer = _make_enforcer()
    enforcer.set_policies_for_testing([p])
    # Should still block the email tool call
    assert enforcer.check_policy(_competitor_email_context()).is_block


# ---- StrathonPolicyBlocked exception ----


def test_blocked_exception_carries_metadata():
    exc = StrathonPolicyBlocked(
        "you shall not pass",
        policy_id="pol_x",
        policy_name="block_rule",
    )
    assert str(exc) == "you shall not pass"
    assert exc.policy_id == "pol_x"
    assert exc.policy_name == "block_rule"
    with pytest.raises(StrathonPolicyBlocked):
        raise exc


# ---- Policy refresh against an unreachable server ----


def test_refresh_against_unreachable_endpoint_returns_false():
    enforcer = PolicyEnforcer(
        endpoint="http://127.0.0.1:1",  # nothing listens here
        request_timeout_sec=0.2,
    )
    assert enforcer.refresh() is False
    assert enforcer.last_refresh_error is not None
    # check_policy still works, just returns ALLOW
    assert enforcer.check_policy({"name": "x", "attrs": {}}).is_allow


# ---- Policy data model ----


def test_policy_serialization_roundtrip():
    p = _block_policy()
    serialized = p.to_dict()
    restored = Policy.from_dict(serialized)
    assert restored.id == p.id
    assert restored.name == p.name
    assert restored.action == p.action
    assert restored.match_expression == p.match_expression
    assert restored.action_config == p.action_config
    assert restored.priority == p.priority

"""Tests for allow-list mode and the ``action: "allow"`` policy.

Two layers:

* Policy decision flow inside ``PolicyEnforcer`` — verify that
  ``action="allow"`` short-circuits subsequent rules and that the
  end-of-iteration default falls back correctly to allow vs block
  based on the project's ``intervention_default_action``.
* Refresh wiring — verify that ``refresh()`` reads
  ``intervention_default_action`` from the receiver's response and
  exposes it via the ``intervention_default_action`` property.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from strathon.policy.enforcer import PolicyEnforcer
from strathon.policy.types import Policy


def _enforcer() -> PolicyEnforcer:
    return PolicyEnforcer(endpoint="http://localhost:0", api_key="test")


def _block_policy(name: str = "default_block", priority: int = 0) -> Policy:
    return Policy(
        id=f"id-{name}",
        project_id="proj",
        name=name,
        match_expression="true",
        action="block",
        action_config={"message": f"blocked by {name}"},
        priority=priority,
    )


def _allow_policy(
    name: str = "default_allow",
    match_expression: str = "true",
    priority: int = 0,
) -> Policy:
    return Policy(
        id=f"id-{name}",
        project_id="proj",
        name=name,
        match_expression=match_expression,
        action="allow",
        priority=priority,
    )


def _ctx(name: str = "any.tool") -> dict:
    return {"name": name, "attrs": {}}


# ---- Default-allow mode (backward compat) --------------------------------


def test_no_policies_no_setting_returns_allow():
    """Constructor default is intervention_default_action='allow'. With
    no policies, every call is admitted — pre-allow-list behavior."""
    e = _enforcer()
    assert e.intervention_default_action == "allow"
    assert e.check_policy(_ctx()).is_allow


def test_unmatched_call_with_default_allow_returns_allow():
    e = _enforcer()
    # A block policy that doesn't match.
    e.set_policies_for_testing([
        Policy(
            id="p1", project_id="proj", name="picky",
            match_expression="name == 'tool.sensitive'",
            action="block",
            action_config={"message": "blocked"},
        ),
    ])
    decision = e.check_policy(_ctx(name="other.tool"))
    assert decision.is_allow


# ---- Allow-list mode (intervention_default_action="block") ---------------


def test_unmatched_call_with_default_block_returns_synthetic_block():
    """The signature feature: unmatched calls get a block decision."""
    e = _enforcer()
    e.set_intervention_default_action_for_testing("block")
    # No policies. Default-block means EVERY call denies.
    decision = e.check_policy(_ctx())
    assert decision.is_block
    assert decision.policy_id is None
    assert decision.policy_name is None
    assert "allow-list mode" in (decision.message or "")


def test_unmatched_call_default_block_with_non_matching_allow_still_blocks():
    """An allow policy whose CEL doesn't match the call doesn't admit
    it. Allow-list mode demands an explicit match."""
    e = _enforcer()
    e.set_intervention_default_action_for_testing("block")
    e.set_policies_for_testing([
        _allow_policy(match_expression="name == 'tool.permitted'"),
    ])
    decision = e.check_policy(_ctx(name="tool.something_else"))
    assert decision.is_block
    assert decision.policy_id is None


def test_matched_allow_admits_call_in_default_block_mode():
    """An allow policy whose CEL matches the call admits it past the
    default-deny."""
    e = _enforcer()
    e.set_intervention_default_action_for_testing("block")
    e.set_policies_for_testing([
        _allow_policy(match_expression="name == 'tool.permitted'"),
    ])
    decision = e.check_policy(_ctx(name="tool.permitted"))
    assert decision.is_allow
    # The decision carries the policy that admitted, useful for audit.
    assert decision.policy_name == "default_allow"


# ---- Allow short-circuits subsequent policies ----------------------------


def test_higher_priority_allow_beats_lower_priority_block():
    """When an allow fires first by priority, the lower-priority block
    is never evaluated."""
    e = _enforcer()
    e.set_policies_for_testing([
        _allow_policy(priority=100),
        _block_policy(priority=1),
    ])
    decision = e.check_policy(_ctx())
    assert decision.is_allow


def test_higher_priority_block_beats_lower_priority_allow():
    """The dual: a higher-priority block wins over a lower-priority
    allow. Priority ordering is preserved across action types."""
    e = _enforcer()
    e.set_policies_for_testing([
        _block_policy(priority=100),
        _allow_policy(priority=1),
    ])
    decision = e.check_policy(_ctx())
    assert decision.is_block


def test_allow_short_circuits_in_default_allow_mode_too():
    """Outside allow-list mode, an explicit allow still short-circuits
    lower-priority rules. This lets operators carve specific tools out
    from a broad block."""
    e = _enforcer()  # default is "allow"
    e.set_policies_for_testing([
        _allow_policy(
            name="allow_ping",
            match_expression="name == 'tool.ping'",
            priority=100,
        ),
        Policy(
            id="block_all", project_id="proj", name="block_all_tools",
            match_expression="name.startsWith('tool.')",
            action="block",
            action_config={"message": "all tools blocked"},
            priority=1,
        ),
    ])
    # ping is admitted.
    assert e.check_policy(_ctx(name="tool.ping")).is_allow
    # everything else hits the block.
    decision = e.check_policy(_ctx(name="tool.search"))
    assert decision.is_block


# ---- Refresh wiring ------------------------------------------------------


def test_refresh_reads_intervention_default_action_from_response():
    """``refresh()`` must pull the field from the server's payload and
    cache it on the enforcer."""
    e = _enforcer()
    assert e.intervention_default_action == "allow"

    payload = {
        "policies": [],
        "intervention_default_action": "block",
    }
    fake_body = json.dumps(payload).encode("utf-8")

    class _FakeResponse:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    with patch(
        "strathon.policy.enforcer.urlopen",
        return_value=_FakeResponse(fake_body),
    ):
        ok = e.refresh()
    assert ok is True
    assert e.intervention_default_action == "block"


def test_refresh_defaults_to_allow_when_server_omits_field():
    """An older receiver that doesn't yet send the field must not flip
    the project into allow-list mode. We default to 'allow'."""
    e = _enforcer()
    e.set_intervention_default_action_for_testing("block")

    # Payload without the new field — what a pre-allow-list receiver returns.
    payload = {"policies": []}
    fake_body = json.dumps(payload).encode("utf-8")

    class _FakeResponse:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    with patch(
        "strathon.policy.enforcer.urlopen",
        return_value=_FakeResponse(fake_body),
    ):
        ok = e.refresh()
    assert ok is True
    assert e.intervention_default_action == "allow"


def test_refresh_ignores_unknown_default_action_value(caplog):
    """A receiver returning a value outside the enum (e.g. typo or
    future value the SDK doesn't recognize) shouldn't crash or
    accidentally lock down agents. Fall back to 'allow' with a warning."""
    e = _enforcer()

    payload = {
        "policies": [],
        "intervention_default_action": "deny",  # not in {allow, block}
    }
    fake_body = json.dumps(payload).encode("utf-8")

    class _FakeResponse:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    with caplog.at_level("WARNING"):
        with patch(
            "strathon.policy.enforcer.urlopen",
            return_value=_FakeResponse(fake_body),
        ):
            ok = e.refresh()
    assert ok is True
    assert e.intervention_default_action == "allow"
    assert any("unknown intervention_default_action" in m for m in caplog.messages)


def test_set_intervention_default_action_for_testing_rejects_bad_value():
    e = _enforcer()
    with pytest.raises(ValueError):
        e.set_intervention_default_action_for_testing("deny")


# ---- Interaction with other actions --------------------------------------


def test_allow_does_not_break_throttle_evaluation_in_priority_order():
    """A higher-priority allow that matches still wins over a
    lower-priority throttle that might have refused."""
    e = _enforcer()
    e.set_policies_for_testing([
        _allow_policy(priority=100),
        Policy(
            id="t", project_id="proj", name="t",
            match_expression="true", action="throttle",
            action_config={"max_calls": 1, "window_seconds": 60},
            priority=1,
        ),
    ])
    # Drain throttle bucket via several calls. None of these reach the
    # throttle policy because allow short-circuits, so all are admitted.
    for _ in range(5):
        assert e.check_policy(_ctx()).is_allow


def test_block_in_allow_list_mode_with_disabled_allow_still_blocks():
    """A disabled allow policy is treated as absent. In allow-list mode
    with the allow disabled, the call hits the synthetic block."""
    e = _enforcer()
    e.set_intervention_default_action_for_testing("block")
    e.set_policies_for_testing([
        Policy(
            id="allow", project_id="proj", name="allow_all",
            match_expression="true", action="allow",
            enabled=False,
            priority=100,
        ),
    ])
    decision = e.check_policy(_ctx())
    assert decision.is_block
    assert decision.policy_id is None  # synthetic, not the disabled allow

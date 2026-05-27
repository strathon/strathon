"""Tests for the halt path through ``dispatch_policy_decision``.

The dispatcher checks halts before policies; an active halt raises
StrathonHaltExceeded at the tool boundary regardless of what the
policy enforcer says. These tests verify:

  * Active halt raises StrathonHaltExceeded; tool body never runs
  * Halt takes precedence over a policy block (the halt fires first)
  * Halt check failure (enforcer raises) falls through to policy check
    — the halt check must not break the user's tool
  * Halt allow + policy block: block still fires (halt path doesn't
    interfere with normal policy enforcement)
  * Audit span carries strathon.halt.* attributes on halt
"""

from __future__ import annotations

import pytest

from strathon.policy import enforce_steer
from strathon.policy.steer import _uninstall_all_for_testing
from strathon.policy.types import (
    ALLOW,
    ALLOW_HALT,
    HaltDecision,
    PolicyDecision,
    StrathonHaltExceeded,
    StrathonPolicyBlocked,
)


# ---- Fakes --------------------------------------------------------------


class _FakeEnforcer:
    pass


class _RecordingSpan:
    def __init__(self, name, attributes):
        self.name = name
        self.attributes = dict(attributes or {})
        self.status = None
        self.ended = False

    def set_status(self, status):
        self.status = status

    def end(self):
        self.ended = True


class _RecordingTracer:
    def __init__(self):
        self.spans = []

    def start_span(self, name, attributes=None):
        span = _RecordingSpan(name, attributes or {})
        self.spans.append(span)
        return span


class _FakeClient:
    """Stand-in for strathon.Client with both check_policy and check_halt."""

    def __init__(
        self,
        *,
        policy_decision: PolicyDecision = ALLOW,
        halt_decision: HaltDecision = ALLOW_HALT,
        halt_raises: bool = False,
    ):
        self._policy_enforcer = _FakeEnforcer()
        self._halt_enforcer = _FakeEnforcer()
        self._policy_decision = policy_decision
        self._halt_decision = halt_decision
        self._halt_raises = halt_raises
        self.tracer = _RecordingTracer()
        self.policy_checks = 0
        self.halt_checks = 0

    def check_policy(self, span):
        self.policy_checks += 1
        return self._policy_decision

    def check_halt(self, span):
        self.halt_checks += 1
        if self._halt_raises:
            raise RuntimeError("simulated halt lookup failure")
        return self._halt_decision


# ---- Fixtures -----------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_steer_state():
    yield
    _uninstall_all_for_testing()


def _make_tool():
    from langchain_core.tools import tool

    @tool
    def reverse(text: str) -> str:
        """Reverse text."""
        return text[::-1]

    return reverse


# ---- Halt path ----------------------------------------------------------


def test_active_halt_raises_strathon_halt_exceeded():
    tool = _make_tool()
    client = _FakeClient(
        halt_decision=HaltDecision(
            action="halt",
            halt_id=42,
            scope="agent",
            scope_value="agent-7",
            reason="killswitch",
            state="halted",
        ),
    )
    enforce_steer(tool, client)

    with pytest.raises(StrathonHaltExceeded) as exc_info:
        tool.invoke({"text": "hello"})

    assert exc_info.value.halt_id == 42
    assert exc_info.value.scope == "agent"
    assert exc_info.value.scope_value == "agent-7"
    assert exc_info.value.reason == "killswitch"


def test_halt_message_includes_tool_name_and_halt_id():
    tool = _make_tool()
    client = _FakeClient(
        halt_decision=HaltDecision(
            action="halt", halt_id=99, scope="project",
            scope_value=None, reason="full stop", state="halted",
        ),
    )
    enforce_steer(tool, client)

    with pytest.raises(StrathonHaltExceeded) as exc_info:
        tool.invoke({"text": "hi"})

    msg = str(exc_info.value)
    assert "halt #99" in msg
    assert "full stop" in msg
    assert "project" in msg


def test_halt_skips_tool_body():
    """If a halt fires, the user's tool body must NEVER execute."""
    calls = []

    from langchain_core.tools import tool

    @tool
    def side_effect(text: str) -> str:
        """Records that it ran."""
        calls.append(text)
        return text

    client = _FakeClient(
        halt_decision=HaltDecision(
            action="halt", halt_id=1, scope="project",
            scope_value=None, reason="stop", state="halted",
        ),
    )
    enforce_steer(side_effect, client)

    with pytest.raises(StrathonHaltExceeded):
        side_effect.invoke({"text": "should not run"})

    assert calls == []  # tool body never ran


def test_halt_takes_precedence_over_policy_block():
    """When a halt is active AND a policy would block, the halt fires
    first. The policy check should not even run (no point evaluating
    CEL on an agent that's supposed to be stopped)."""
    tool = _make_tool()
    client = _FakeClient(
        halt_decision=HaltDecision(
            action="halt", halt_id=1, scope="project",
            scope_value=None, reason="halted", state="halted",
        ),
        policy_decision=PolicyDecision(
            action="block",
            policy_id="pol_1",
            policy_name="should_not_fire",
            message="should not see this",
        ),
    )
    enforce_steer(tool, client)

    with pytest.raises(StrathonHaltExceeded):
        tool.invoke({"text": "x"})

    assert client.halt_checks == 1
    # Policy check NEVER ran — halt fired first
    assert client.policy_checks == 0


def test_halt_check_failure_falls_through_to_policy():
    """If client.check_halt raises (a bug in halt code), the dispatcher
    must not break the user's tool. It logs and proceeds to the policy
    check as if no halt were active."""
    tool = _make_tool()
    client = _FakeClient(
        halt_raises=True,
        policy_decision=ALLOW,
    )
    enforce_steer(tool, client)

    # No exception raised; the tool body runs normally.
    result = tool.invoke({"text": "abc"})
    assert result == "cba"

    # Halt was attempted, policy check then ran
    assert client.halt_checks == 1
    assert client.policy_checks == 1


def test_halt_allow_does_not_interfere_with_policy_block():
    """No halt active + policy block: block still fires normally.
    The halt path is purely additive."""
    tool = _make_tool()
    client = _FakeClient(
        halt_decision=ALLOW_HALT,
        policy_decision=PolicyDecision(
            action="block",
            policy_name="blocker",
            message="blocked by rule",
        ),
    )
    enforce_steer(tool, client)

    with pytest.raises(StrathonPolicyBlocked):
        tool.invoke({"text": "x"})

    assert client.halt_checks == 1
    assert client.policy_checks == 1


def test_halt_allow_does_not_interfere_with_steer():
    """No halt + policy steer: replacement string is returned as normal."""
    tool = _make_tool()
    client = _FakeClient(
        halt_decision=ALLOW_HALT,
        policy_decision=PolicyDecision(
            action="steer",
            replacement="[REDACTED]",
            policy_name="redact",
        ),
    )
    enforce_steer(tool, client)

    result = tool.invoke({"text": "secret"})
    assert result == "[REDACTED]"


def test_halt_emits_intervention_span_with_halt_attrs():
    """The halt path emits an audit span like block does, with halt-
    specific attributes the receiver can index."""
    tool = _make_tool()
    client = _FakeClient(
        halt_decision=HaltDecision(
            action="halt",
            halt_id=42,
            scope="agent",
            scope_value="agent-7",
            reason="killswitch",
            state="halted",
        ),
    )
    enforce_steer(tool, client)

    with pytest.raises(StrathonHaltExceeded):
        tool.invoke({"text": "x"})

    # One audit span was emitted
    assert len(client.tracer.spans) == 1
    span = client.tracer.spans[0]
    assert span.attributes.get("strathon.policy.halted") is True
    assert span.attributes.get("strathon.halt.id") == 42
    assert span.attributes.get("strathon.halt.scope") == "agent"
    assert span.attributes.get("strathon.halt.scope_value") == "agent-7"
    assert span.attributes.get("strathon.halt.reason") == "killswitch"
    # Halt is an error-state outcome like block
    assert span.status is not None


def test_allow_halt_emits_no_intervention_span():
    """Pure allow path doesn't emit an audit span (would be noise)."""
    tool = _make_tool()
    client = _FakeClient(
        halt_decision=ALLOW_HALT,
        policy_decision=ALLOW,
    )
    enforce_steer(tool, client)

    result = tool.invoke({"text": "abc"})
    assert result == "cba"
    assert client.tracer.spans == []

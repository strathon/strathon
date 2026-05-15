"""Tests for ``strathon.instrumentation.openai_agents.strathon_tool_guardrail``
and ``attach_strathon_guardrails``.

These tests exercise the OpenAI Agents Tool Guardrails path without
spinning up a real Runner — we invoke the guardrail function directly
with a constructed ToolInputGuardrailData. That's the seam at which the
production runtime calls into our code, so testing here covers what
matters without needing a live model.

Block, steer, and allow each have a dedicated test. Idempotent
attachment to an agent, isolation from non-FunctionTool entries, and
failure-isolation (a broken policy check must not break the user's
agent) are covered explicitly.
"""

from __future__ import annotations

import asyncio
import pytest

from strathon.policy.types import PolicyDecision


# ---- Fake client ---------------------------------------------------------


class _FakeEnforcer:
    pass


class _FakeClient:
    def __init__(self, decision: PolicyDecision, *, raise_on_check: bool = False) -> None:
        self._policy_enforcer = _FakeEnforcer()
        self._decision = decision
        self._raise = raise_on_check
        self.call_count = 0

    def check_policy(self, span):
        self.call_count += 1
        if self._raise:
            raise RuntimeError("simulated policy lookup failure")
        return self._decision


# ---- ToolInputGuardrailData shim ----------------------------------------
#
# The real ToolInputGuardrailData wraps a ToolContext that holds a
# tool_call with .name and .arguments. We fake those minimally so we
# can invoke the guardrail function in isolation. The fields the
# guardrail reads are name + arguments — nothing else.


class _FakeToolCall:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolContext:
    def __init__(self, tool_call: _FakeToolCall) -> None:
        self.tool_call = tool_call


class _FakeData:
    def __init__(self, tool_call: _FakeToolCall) -> None:
        self.context = _FakeToolContext(tool_call)
        self.agent = None


def _data(name: str = "send_email", args: str = '{"to": "x@y.com"}') -> _FakeData:
    return _FakeData(_FakeToolCall(name=name, arguments=args))


# ---- Block ---------------------------------------------------------------


def test_guardrail_block_returns_raise_exception_behavior():
    from strathon.instrumentation.openai_agents import strathon_tool_guardrail

    client = _FakeClient(
        PolicyDecision(
            action="block",
            policy_id="pol_1",
            policy_name="no_comp",
            message="blocked",
        ),
    )
    guardrail = strathon_tool_guardrail(client)

    out = asyncio.run(guardrail.guardrail_function(_data()))

    # The runtime checks .behavior["type"]; raise_exception triggers
    # ToolInputGuardrailTripwireTriggered upstream.
    assert out.behavior["type"] == "raise_exception"
    assert out.output_info["policy_id"] == "pol_1"
    assert out.output_info["policy_name"] == "no_comp"


# ---- Steer ---------------------------------------------------------------


def test_guardrail_steer_returns_reject_content_with_replacement():
    from strathon.instrumentation.openai_agents import strathon_tool_guardrail

    client = _FakeClient(
        PolicyDecision(
            action="steer",
            replacement="[REDACTED]",
            policy_name="redact",
        ),
    )
    guardrail = strathon_tool_guardrail(client)

    out = asyncio.run(guardrail.guardrail_function(_data()))

    # reject_content: the runtime substitutes .behavior["message"] for
    # the tool's natural output. Exactly the steer contract.
    assert out.behavior["type"] == "reject_content"
    assert out.behavior["message"] == "[REDACTED]"


def test_guardrail_steer_falls_back_when_replacement_missing():
    from strathon.instrumentation.openai_agents import strathon_tool_guardrail

    client = _FakeClient(
        PolicyDecision(
            action="steer",
            replacement=None,
            policy_name="vague",
        ),
    )
    guardrail = strathon_tool_guardrail(client)

    out = asyncio.run(guardrail.guardrail_function(_data()))

    assert out.behavior["type"] == "reject_content"
    # Fallback names the tool and policy so the user can debug
    assert "vague" in out.behavior["message"]
    assert "Strathon" in out.behavior["message"]


# ---- Allow ---------------------------------------------------------------


def test_guardrail_allow_returns_allow_behavior():
    from strathon.instrumentation.openai_agents import strathon_tool_guardrail

    client = _FakeClient(PolicyDecision(action="allow"))
    guardrail = strathon_tool_guardrail(client)

    out = asyncio.run(guardrail.guardrail_function(_data()))

    assert out.behavior["type"] == "allow"


# ---- No enforcer on client (policies disabled) -------------------------


def test_guardrail_with_no_enforcer_allows_through():
    """If the client has policies disabled (_policy_enforcer is None),
    the guardrail must transparently allow every call."""
    from strathon.instrumentation.openai_agents import strathon_tool_guardrail

    class _NoEnforcerClient:
        _policy_enforcer = None  # policies disabled
        def check_policy(self, span):
            pytest.fail("check_policy should not be called when enforcer is None")

    guardrail = strathon_tool_guardrail(_NoEnforcerClient())
    out = asyncio.run(guardrail.guardrail_function(_data()))
    assert out.behavior["type"] == "allow"


# ---- Failure isolation ---------------------------------------------------


def test_policy_check_exception_falls_back_to_allow():
    """A bug in policy evaluation must never break the user's agent."""
    from strathon.instrumentation.openai_agents import strathon_tool_guardrail

    client = _FakeClient(PolicyDecision(action="block"), raise_on_check=True)
    guardrail = strathon_tool_guardrail(client)

    out = asyncio.run(guardrail.guardrail_function(_data()))

    # check_policy raised; we degrade to allow rather than crash
    assert out.behavior["type"] == "allow"
    assert client.call_count == 1  # we did try once


# ---- attach_strathon_guardrails -----------------------------------------


def test_attach_strathon_guardrails_walks_all_function_tools():
    from agents import function_tool, Agent
    from strathon.instrumentation.openai_agents import attach_strathon_guardrails

    @function_tool
    def t1(x: str) -> str:
        """tool 1"""
        return x

    @function_tool
    def t2(y: int) -> int:
        """tool 2"""
        return y

    agent = Agent(name="multi", tools=[t1, t2])
    client = _FakeClient(PolicyDecision(action="allow"))

    n = attach_strathon_guardrails(agent, client)

    assert n == 2
    assert len(t1.tool_input_guardrails) == 1
    assert len(t2.tool_input_guardrails) == 1
    assert t1.tool_input_guardrails[0].name == "strathon_policy"


def test_attach_strathon_guardrails_is_idempotent():
    """Attaching twice mustn't stack duplicate guardrails."""
    from agents import function_tool, Agent
    from strathon.instrumentation.openai_agents import attach_strathon_guardrails

    @function_tool
    def t(x: str) -> str:
        """tool"""
        return x

    agent = Agent(name="dup", tools=[t])
    client = _FakeClient(PolicyDecision(action="allow"))

    n1 = attach_strathon_guardrails(agent, client)
    n2 = attach_strathon_guardrails(agent, client)

    assert n1 == 1
    assert n2 == 0  # nothing new to attach
    assert len(t.tool_input_guardrails) == 1


def test_attach_strathon_guardrails_preserves_existing_guardrails():
    """A user-defined guardrail already on a tool must stay attached."""
    from agents import function_tool, Agent
    from agents.tool_guardrails import ToolInputGuardrail, ToolGuardrailFunctionOutput
    from strathon.instrumentation.openai_agents import attach_strathon_guardrails

    async def _user_guardrail(data):
        return ToolGuardrailFunctionOutput.allow()

    user_g = ToolInputGuardrail(guardrail_function=_user_guardrail, name="user_custom")

    @function_tool
    def t(x: str) -> str:
        """tool"""
        return x

    t.tool_input_guardrails = [user_g]
    agent = Agent(name="coexist", tools=[t])
    client = _FakeClient(PolicyDecision(action="allow"))

    attach_strathon_guardrails(agent, client)

    names = [g.name for g in t.tool_input_guardrails]
    assert "user_custom" in names
    assert "strathon_policy" in names
    assert len(names) == 2

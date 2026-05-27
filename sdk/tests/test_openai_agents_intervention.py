"""Integration tests: OpenAI Agents SDK runtime intervention.

The block point for OAI Agents SDK is RunHooks.on_tool_start, which the
runner awaits via asyncio.gather() BEFORE creating the tool invocation task.
Raising from on_tool_start propagates through gather and prevents the tool
from running.

We exercise the hook directly (no real Runner.run, which would require an
OpenAI API key). The Runner-wrapping path is exercised by the wiring tests
that confirm the original methods are saved and restored cleanly.
"""

import asyncio
import pytest

from strathon import Client
from strathon.policy import Policy, StrathonPolicyBlocked
from strathon.policy.enforcer import PolicyEnforcer


pytest.importorskip("agents")


def _make_client_with_policy(policy: Policy) -> Client:
    client = Client(
        api_key="test-key",
        endpoint="http://localhost:4318",
        set_global_tracer=False,
        enable_policies=False,
    )
    enforcer = PolicyEnforcer(
        endpoint="http://localhost:4318",
        api_key="test-key",
        project_id="00000000-0000-0000-0000-000000000001",
    )
    enforcer.set_policies_for_testing([policy])
    client._policy_enforcer = enforcer
    return client


def _block_competitor_policy() -> Policy:
    return Policy(
        id="pol_no_competitor",
        project_id="00000000-0000-0000-0000-000000000001",
        name="block_competitor_email",
        match_expression=(
            'attrs["gen_ai.tool.name"] == "send_email" && '
            'attrs["strathon.tool.args"].contains("@competitor.com")'
        ),
        action="block",
        action_config={"message": "Cannot email a competitor address."},
    )


def _steer_competitor_policy() -> Policy:
    return Policy(
        id="pol_steer_competitor",
        project_id="00000000-0000-0000-0000-000000000001",
        name="steer_competitor_email",
        match_expression='attrs["strathon.tool.args"].contains("competitor")',
        action="steer",
        action_config={"replacement": "Use the internal alternative."},
    )


class _FakeToolContext:
    """Minimal stand-in for ToolContext that exposes what our hook reads."""

    def __init__(self, tool_arguments=None):
        self.tool_arguments = tool_arguments


class _FakeTool:
    def __init__(self, name):
        self.name = name


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Clean instrumentation state before and after each test."""
    import strathon.instrumentation.openai_agents as mod
    mod._uninstall_policy_patch()
    yield
    mod._uninstall_policy_patch()


# ---- Hook: block path ----


def test_hook_blocks_when_policy_matches():
    """on_tool_start must raise StrathonPolicyBlocked when policy matches."""
    from strathon.instrumentation.openai_agents import _build_strathon_run_hooks

    client = _make_client_with_policy(_block_competitor_policy())
    hooks = _build_strathon_run_hooks(client, user_hooks=None)

    context = _FakeToolContext(tool_arguments={"to": "sales@competitor.com"})
    tool = _FakeTool("send_email")

    async def go():
        await hooks.on_tool_start(context, agent=None, tool=tool)

    with pytest.raises(StrathonPolicyBlocked) as exc_info:
        asyncio.run(go())

    assert "Cannot email a competitor address" in str(exc_info.value)
    assert exc_info.value.policy_name == "block_competitor_email"


# ---- Hook: allow path ----


def test_hook_allows_when_policy_does_not_match():
    from strathon.instrumentation.openai_agents import _build_strathon_run_hooks

    client = _make_client_with_policy(_block_competitor_policy())
    hooks = _build_strathon_run_hooks(client, user_hooks=None)

    context = _FakeToolContext(tool_arguments={"to": "team@mycompany.com"})
    tool = _FakeTool("send_email")

    async def go():
        await hooks.on_tool_start(context, agent=None, tool=tool)

    # Should complete without raising
    asyncio.run(go())


# ---- Hook: steer is not supported on OAI; logs but does not raise ----


def test_hook_steer_via_runhooks_does_not_raise_and_logs_warning(caplog):
    """The RunHooks path cannot substitute tool output, so on a steer
    match it logs a warning telling the user to attach the guardrail
    helper, then lets the tool proceed.

    Real steer enforcement on OpenAI Agents SDK lives in
    attach_strathon_guardrails (covered in test_openai_agents_steer.py)."""
    from strathon.instrumentation.openai_agents import _build_strathon_run_hooks
    import logging

    client = _make_client_with_policy(_steer_competitor_policy())
    hooks = _build_strathon_run_hooks(client, user_hooks=None)

    context = _FakeToolContext(tool_arguments={"to": "sales@competitor.com"})
    tool = _FakeTool("send_email")

    async def go():
        await hooks.on_tool_start(context, agent=None, tool=tool)

    with caplog.at_level(logging.WARNING, logger="strathon.instrumentation.openai_agents"):
        asyncio.run(go())

    # Did not raise; warning logged that points the user at the guardrail path.
    assert any("attach_strathon_guardrails" in r.message for r in caplog.records)


# ---- Hook: policy exception doesn't break the tool ----


def test_hook_policy_check_exception_does_not_propagate():
    """If check_policy itself raises, on_tool_start must NOT raise."""
    from strathon.instrumentation.openai_agents import _build_strathon_run_hooks

    client = _make_client_with_policy(_block_competitor_policy())
    # Sabotage the client's check_policy
    def _boom(span_context):
        raise RuntimeError("synthetic policy failure")
    client.check_policy = _boom

    hooks = _build_strathon_run_hooks(client, user_hooks=None)
    context = _FakeToolContext(tool_arguments={"to": "anyone@anywhere.com"})
    tool = _FakeTool("send_email")

    async def go():
        await hooks.on_tool_start(context, agent=None, tool=tool)

    # Must NOT raise — broken enforcer fails open
    asyncio.run(go())


# ---- Hook: delegates to user's hooks ----


def test_hook_delegates_to_user_hooks():
    """If the user passed their own hooks, ours should call theirs after."""
    from agents import RunHooks
    from strathon.instrumentation.openai_agents import _build_strathon_run_hooks

    calls = []

    class UserHooks(RunHooks):
        async def on_tool_start(self, context, agent, tool):
            calls.append("tool_start")

        async def on_agent_start(self, context, agent):
            calls.append("agent_start")

    client = _make_client_with_policy(_block_competitor_policy())
    hooks = _build_strathon_run_hooks(client, user_hooks=UserHooks())

    context = _FakeToolContext(tool_arguments={"to": "team@mycompany.com"})
    tool = _FakeTool("send_email")

    async def go():
        await hooks.on_tool_start(context, agent=None, tool=tool)
        await hooks.on_agent_start(None, None)

    asyncio.run(go())
    assert calls == ["tool_start", "agent_start"]


def test_hook_does_not_delegate_when_blocking():
    """When a tool is blocked, user's on_tool_start should NOT be called."""
    from agents import RunHooks
    from strathon.instrumentation.openai_agents import _build_strathon_run_hooks

    calls = []

    class UserHooks(RunHooks):
        async def on_tool_start(self, context, agent, tool):
            calls.append("tool_start")

    client = _make_client_with_policy(_block_competitor_policy())
    hooks = _build_strathon_run_hooks(client, user_hooks=UserHooks())

    context = _FakeToolContext(tool_arguments={"to": "sales@competitor.com"})
    tool = _FakeTool("send_email")

    async def go():
        await hooks.on_tool_start(context, agent=None, tool=tool)

    with pytest.raises(StrathonPolicyBlocked):
        asyncio.run(go())

    assert calls == []  # user's hook never reached


# ---- Runner wrapping ----


def test_install_policy_patch_replaces_runner_classmethods():
    from agents import Runner
    from strathon.instrumentation.openai_agents import (
        _install_policy_patch, _uninstall_policy_patch,
    )

    # Classmethods give fresh bound objects per access; compare __func__
    pre_run_func = Runner.run.__func__
    pre_run_sync_func = Runner.run_sync.__func__
    pre_run_streamed_func = Runner.run_streamed.__func__

    client = _make_client_with_policy(_block_competitor_policy())
    assert _install_policy_patch(client) is True

    # All three classmethods replaced
    assert Runner.run.__func__ is not pre_run_func
    assert Runner.run_sync.__func__ is not pre_run_sync_func
    assert Runner.run_streamed.__func__ is not pre_run_streamed_func

    _uninstall_policy_patch()
    # Restored
    assert Runner.run.__func__ is pre_run_func
    assert Runner.run_sync.__func__ is pre_run_sync_func
    assert Runner.run_streamed.__func__ is pre_run_streamed_func


def test_install_policy_patch_is_idempotent():
    from strathon.instrumentation.openai_agents import _install_policy_patch
    import strathon.instrumentation.openai_agents as mod

    client1 = _make_client_with_policy(_block_competitor_policy())
    _install_policy_patch(client1)
    original_after_first = mod._ORIGINAL_RUN

    client2 = _make_client_with_policy(_block_competitor_policy())
    _install_policy_patch(client2)
    original_after_second = mod._ORIGINAL_RUN

    # Same saved original — patch not stacked
    assert original_after_first is original_after_second
    # Routing target updated
    assert mod._PATCHED_CLIENT is client2


def test_install_policy_patch_noop_when_policies_disabled():
    from agents import Runner
    from strathon.instrumentation.openai_agents import _install_policy_patch

    # Classmethod bound objects aren't `is`-comparable across accesses; compare __func__
    pre_run_func = Runner.run.__func__

    client = Client(
        api_key="test-key",
        endpoint="http://localhost:4318",
        set_global_tracer=False,
        enable_policies=False,
    )
    assert _install_policy_patch(client) is False
    assert Runner.run.__func__ is pre_run_func  # untouched

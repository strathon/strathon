"""Integration test: real LangChain tool execution blocked by a Strathon policy.

This is the end-to-end proof that runtime intervention works. We:
1. Build a Client with policies disabled (so no network calls)
2. Manually seed the policy enforcer with a block rule
3. Get the LangGraph callback handler
4. Invoke a real LangChain tool with the handler attached
5. Assert the tool was prevented from running and StrathonPolicyBlocked
   was raised
"""

import pytest

from strathon import Client
from strathon.policy import Policy, StrathonPolicyBlocked
from strathon.policy.enforcer import PolicyEnforcer


pytest.importorskip("langchain_core")
from langchain_core.tools import tool  # noqa: E402


@tool
def send_email(to: str, body: str) -> str:
    """Send an email."""
    # BaseTool doesn't formally declare _executed / _last_to in its
    # interface; we attach them at runtime as test-instrumentation
    # state. The assertions below read the same attrs. Mypy can't see
    # this dynamic shape so we localize the ignores to the attrs.
    send_email._executed = True  # type: ignore[attr-defined]
    send_email._last_to = to  # type: ignore[attr-defined]
    return f"sent to {to}"


def _reset_tool():
    send_email._executed = False  # type: ignore[attr-defined]
    send_email._last_to = None  # type: ignore[attr-defined]


def _make_client_with_block_policy():
    """Build a Client, swap in a hand-rolled PolicyEnforcer with our test rule."""
    client = Client(
        api_key="test-key",
        endpoint="http://localhost:4318",
        set_global_tracer=False,
        enable_policies=False,  # we'll inject manually
    )
    enforcer = PolicyEnforcer(
        endpoint="http://localhost:4318",
        api_key="test-key",
        project_id="00000000-0000-0000-0000-000000000001",
    )
    enforcer.set_policies_for_testing([
        Policy(
            id="pol_no_competitor_email",
            project_id="00000000-0000-0000-0000-000000000001",
            name="block_competitor_email",
            match_expression=(
                'attrs["gen_ai.tool.name"] == "send_email" && '
                'attrs["strathon.tool.args"].contains("@competitor.com")'
            ),
            action="block",
            action_config={"message": "Cannot email a competitor address."},
        ),
    ])
    client._policy_enforcer = enforcer
    return client


def test_real_langchain_tool_is_blocked_by_policy():
    from strathon.instrumentation.langgraph import instrument

    _reset_tool()
    client = _make_client_with_block_policy()

    # Re-instrument cleanly: reset module singleton
    import strathon.instrumentation.langgraph as mod
    mod._REGISTERED_HANDLER = None
    handler = instrument(client)
    assert handler is not None

    # Calling with a competitor address must raise and the tool must not run
    with pytest.raises(StrathonPolicyBlocked) as exc_info:
        send_email.invoke(
            {"to": "sales@competitor.com", "body": "hello"},
            config={"callbacks": [handler]},
        )

    assert "Cannot email a competitor address" in str(exc_info.value)
    assert exc_info.value.policy_name == "block_competitor_email"
    assert send_email._executed is False  # type: ignore[attr-defined]
    assert send_email._last_to is None  # type: ignore[attr-defined]


def test_real_langchain_tool_runs_when_policy_does_not_match():
    from strathon.instrumentation.langgraph import instrument

    _reset_tool()
    client = _make_client_with_block_policy()
    import strathon.instrumentation.langgraph as mod
    mod._REGISTERED_HANDLER = None
    handler = instrument(client)

    # Innocuous address should pass through
    result = send_email.invoke(
        {"to": "team@mycompany.com", "body": "hello"},
        config={"callbacks": [handler]},
    )
    assert send_email._executed is True  # type: ignore[attr-defined]
    assert send_email._last_to == "team@mycompany.com"  # type: ignore[attr-defined]
    assert "team@mycompany.com" in result


def test_real_langchain_tool_blocked_with_no_policies_passes_through():
    """Sanity: an enforcer with zero policies should let everything through."""
    from strathon.instrumentation.langgraph import instrument

    _reset_tool()
    client = Client(
        api_key="test-key",
        endpoint="http://localhost:4318",
        set_global_tracer=False,
        enable_policies=False,
    )
    enforcer = PolicyEnforcer(
        endpoint="http://localhost:4318",
        api_key="test-key",
    )
    enforcer.set_policies_for_testing([])  # no policies
    client._policy_enforcer = enforcer

    import strathon.instrumentation.langgraph as mod
    mod._REGISTERED_HANDLER = None
    handler = instrument(client)

    result = send_email.invoke(
        {"to": "sales@competitor.com", "body": "hello"},
        config={"callbacks": [handler]},
    )
    assert send_email._executed is True
    assert "competitor.com" in result

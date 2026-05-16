"""Integration tests: real CrewAI tool execution blocked by a Strathon policy.

Mirrors the LangGraph integration test pattern (test_policy_integration.py).
The block point for CrewAI is CrewStructuredTool.invoke, patched at
instrument() time.
"""

import pytest

from strathon import Client
from strathon.policy import Policy, StrathonPolicyBlocked
from strathon.policy.enforcer import PolicyEnforcer


pytest.importorskip("crewai")

from crewai.tools import BaseTool  # noqa: E402


class SendEmailTool(BaseTool):
    name: str = "send_email"
    description: str = "Send an email to a recipient"

    def _run(self, to: str = "", body: str = "") -> str:
        # _sent is class-level state we attach below for the test to
        # inspect; BaseTool doesn't declare it. Mypy can't see the
        # runtime monkey-attach, so the dynamic-attribute reads are
        # marked locally.
        SendEmailTool._sent.append({"to": to, "body": body})  # type: ignore[attr-defined]
        return f"sent to {to}"


SendEmailTool._sent = []  # type: ignore[attr-defined]


def _reset_tool() -> None:
    SendEmailTool._sent = []  # type: ignore[attr-defined]


def _make_client_with_policy(policy: Policy) -> Client:
    """Build a Client and inject a hand-rolled PolicyEnforcer with one policy."""
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
        match_expression=(
            'attrs["gen_ai.tool.name"] == "send_email" && '
            'attrs["strathon.tool.args"].contains("@competitor.com")'
        ),
        action="steer",
        action_config={
            "replacement": "REDIRECTED: Use the internal alternative instead.",
        },
    )


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Make sure each test starts with a clean instrumentation state."""
    import strathon.instrumentation.crewai as mod

    mod._uninstall_policy_patch()
    mod._REGISTERED_LISTENER = None
    _reset_tool()
    yield
    mod._uninstall_policy_patch()
    mod._REGISTERED_LISTENER = None


# ---- Block path ----


def test_real_crewai_tool_is_blocked_when_policy_matches():
    from strathon.instrumentation.crewai import instrument

    client = _make_client_with_policy(_block_competitor_policy())
    assert instrument(client) is True

    structured = SendEmailTool().to_structured_tool()

    with pytest.raises(StrathonPolicyBlocked) as exc_info:
        structured.invoke({"to": "sales@competitor.com", "body": "hi"})

    assert "Cannot email a competitor address" in str(exc_info.value)
    assert exc_info.value.policy_name == "block_competitor_email"
    assert SendEmailTool._sent == []  # type: ignore[attr-defined]  # tool body never executed  # type: ignore[attr-defined]


def test_real_crewai_tool_runs_when_policy_does_not_match():
    from strathon.instrumentation.crewai import instrument

    client = _make_client_with_policy(_block_competitor_policy())
    instrument(client)

    structured = SendEmailTool().to_structured_tool()
    result = structured.invoke({"to": "team@mycompany.com", "body": "hi"})

    assert "team@mycompany.com" in result
    assert len(SendEmailTool._sent) == 1  # type: ignore[attr-defined]
    assert SendEmailTool._sent[0]["to"] == "team@mycompany.com"  # type: ignore[attr-defined]


# ---- Steer path ----


def test_steer_policy_replaces_tool_output():
    """Steer should skip the real tool and return the replacement string."""
    from strathon.instrumentation.crewai import instrument

    client = _make_client_with_policy(_steer_competitor_policy())
    instrument(client)

    structured = SendEmailTool().to_structured_tool()
    result = structured.invoke({"to": "sales@competitor.com", "body": "hi"})

    # Real tool body never ran
    assert SendEmailTool._sent == []  # type: ignore[attr-defined]
    # Agent receives the corrective replacement
    assert result == "REDIRECTED: Use the internal alternative instead."


# ---- Allow path (no enforcement) ----


def test_no_policy_enforcer_means_no_patch():
    """When enable_policies=False on the client, the patch is a no-op."""
    from strathon.instrumentation.crewai import instrument

    client = Client(
        api_key="test-key",
        endpoint="http://localhost:4318",
        set_global_tracer=False,
        enable_policies=False,
    )
    # client has no _policy_enforcer; instrument still returns True for the
    # event listener but the patch is skipped
    instrument(client)

    structured = SendEmailTool().to_structured_tool()
    result = structured.invoke({"to": "anyone@anywhere.com", "body": "hi"})

    assert len(SendEmailTool._sent) == 1  # type: ignore[attr-defined]
    assert "anyone@anywhere.com" in result


# ---- Idempotency ----


def test_double_instrument_does_not_double_patch():
    """Calling instrument() twice must not stack the patch."""
    import strathon.instrumentation.crewai as mod
    from strathon.instrumentation.crewai import instrument

    client1 = _make_client_with_policy(_block_competitor_policy())
    instrument(client1)
    original_after_first = mod._ORIGINAL_INVOKE

    client2 = _make_client_with_policy(_block_competitor_policy())
    instrument(client2)
    original_after_second = mod._ORIGINAL_INVOKE

    # Same saved original — patch was not applied twice
    assert original_after_first is original_after_second
    # New client is now the routing target
    assert mod._PATCHED_CLIENT is client2


def test_retarget_after_double_instrument_uses_new_client():
    """After two instrument() calls, check_policy should hit the second client."""
    from strathon.instrumentation.crewai import instrument

    # First client: policy that blocks
    client1 = _make_client_with_policy(_block_competitor_policy())
    instrument(client1)

    # Second client: NO matching policy — allow everything
    permissive_policy = Policy(
        id="pol_noop",
        project_id="00000000-0000-0000-0000-000000000001",
        name="never_matches",
        match_expression='name == "this.never.matches"',
        action="block",
    )
    client2 = _make_client_with_policy(permissive_policy)
    instrument(client2)

    # The competitor email should now go through — client2 is in charge
    structured = SendEmailTool().to_structured_tool()
    result = structured.invoke({"to": "sales@competitor.com", "body": "hi"})

    assert len(SendEmailTool._sent) == 1  # type: ignore[attr-defined]
    assert "sales@competitor.com" in result


# ---- Robustness ----


def test_policy_check_exception_does_not_break_tool():
    """If check_policy() itself raises, the tool must still run."""
    from strathon.instrumentation.crewai import instrument

    client = _make_client_with_policy(_block_competitor_policy())
    instrument(client)

    # Sabotage the enforcer so check_policy raises an unexpected error
    def _boom(span_context):
        raise RuntimeError("synthetic enforcer failure")

    client.check_policy = _boom

    structured = SendEmailTool().to_structured_tool()
    # Even with a broken enforcer, the tool runs normally
    result = structured.invoke({"to": "anyone@anywhere.com", "body": "hi"})

    assert len(SendEmailTool._sent) == 1  # type: ignore[attr-defined]
    assert "anyone@anywhere.com" in result


# ---- Uninstall ----


def test_uninstall_restores_original_invoke():
    from strathon.instrumentation.crewai import instrument, _uninstall_policy_patch
    from crewai.tools.structured_tool import CrewStructuredTool

    pre_patch = CrewStructuredTool.invoke

    client = _make_client_with_policy(_block_competitor_policy())
    instrument(client)
    assert CrewStructuredTool.invoke is not pre_patch  # patched

    _uninstall_policy_patch()
    assert CrewStructuredTool.invoke is pre_patch  # restored

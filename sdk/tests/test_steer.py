"""Tests for ``strathon.policy.steer.enforce_steer``.

These tests exercise the per-tool enforce_steer path end-to-end against
real LangChain tools (created via ``@tool``). They do NOT spin up a
real receiver — the Strathon Client is replaced by a fake that returns
preconfigured PolicyDecisions, so the only behavior under test is the
class-level invoke patch and the registry.

Block, steer, and allow each have a dedicated test. Idempotent
enrollment, disable, and the failure-isolation contract (a broken
policy check must not break the user's tool) are covered explicitly.
"""

from __future__ import annotations

import pytest

from strathon.policy import enforce_steer, disable_steer
from strathon.policy.steer import _uninstall_all_for_testing
from strathon.policy.types import PolicyDecision, StrathonPolicyBlocked


# ---- Fake client ---------------------------------------------------------


class _FakeEnforcer:
    """Marker so client._policy_enforcer is truthy. Real type is irrelevant —
    enforce_steer only checks for None."""
    pass


class _FakeClient:
    """Minimal stand-in for strathon.Client.

    enforce_steer reads ``_policy_enforcer`` (must be non-None for the
    patch to fire) and calls ``check_policy``. Nothing else.
    """

    def __init__(self, decision: PolicyDecision, *, raise_on_check: bool = False) -> None:
        self._policy_enforcer = _FakeEnforcer()
        self._decision = decision
        self._raise = raise_on_check
        self.call_count = 0
        self.last_span = None

    def check_policy(self, span):
        self.call_count += 1
        self.last_span = span
        if self._raise:
            raise RuntimeError("simulated policy lookup failure")
        return self._decision


# ---- Cleanup -------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_steer_state():
    """Each test starts with a clean registry and unpatched classes.

    Without this, the class-level patch from one test leaks into the
    next and the assertions fail unpredictably.
    """
    yield
    _uninstall_all_for_testing()


def _make_tool():
    """Build a fresh langchain-core tool. Each test gets a new one so the
    enrollment registry can't collide across tests."""
    from langchain_core.tools import tool

    @tool
    def reverse(text: str) -> str:
        """Return text reversed."""
        return text[::-1]

    return reverse


# ---- Block ---------------------------------------------------------------


def test_block_raises_strathon_policy_blocked():
    tool = _make_tool()
    client = _FakeClient(
        PolicyDecision(
            action="block",
            policy_id="pol_1",
            policy_name="no_reversal",
            message="reversal is forbidden",
        ),
    )
    enforce_steer(tool, client)

    with pytest.raises(StrathonPolicyBlocked) as exc_info:
        tool.invoke({"text": "hello"})

    assert exc_info.value.policy_id == "pol_1"
    assert exc_info.value.policy_name == "no_reversal"
    assert "reversal is forbidden" in str(exc_info.value)


def test_block_message_falls_back_when_decision_message_missing():
    tool = _make_tool()
    client = _FakeClient(PolicyDecision(action="block"))  # no message
    enforce_steer(tool, client)

    with pytest.raises(StrathonPolicyBlocked) as exc_info:
        tool.invoke({"text": "hi"})

    # Fallback message names the tool so the user knows what was blocked
    assert "reverse" in str(exc_info.value)


# ---- Steer ---------------------------------------------------------------


def test_steer_returns_replacement_and_skips_body():
    tool = _make_tool()
    client = _FakeClient(
        PolicyDecision(
            action="steer",
            replacement="[REDACTED]",
            policy_name="redact",
        ),
    )
    enforce_steer(tool, client)

    result = tool.invoke({"text": "secret"})

    # The replacement is returned instead of the tool's actual output
    # (which would have been "terces"). This is the steer contract.
    assert result == "[REDACTED]"


def test_steer_synthesizes_fallback_replacement_when_none_provided():
    tool = _make_tool()
    client = _FakeClient(
        PolicyDecision(
            action="steer",
            replacement=None,
            policy_name="vague_steer",
        ),
    )
    enforce_steer(tool, client)

    result = tool.invoke({"text": "x"})

    # Fallback should at least name the policy so the user can debug
    assert "vague_steer" in result
    assert "Strathon" in result


# ---- Allow ---------------------------------------------------------------


def test_allow_runs_the_real_tool_body():
    tool = _make_tool()
    client = _FakeClient(PolicyDecision(action="allow"))
    enforce_steer(tool, client)

    result = tool.invoke({"text": "abc"})

    # Tool body ran; we get the actual reversed value
    assert result == "cba"


# ---- Span context the policy engine sees --------------------------------


def test_check_policy_receives_expected_attrs():
    tool = _make_tool()
    client = _FakeClient(PolicyDecision(action="allow"))
    enforce_steer(tool, client)

    tool.invoke({"text": "abc"})

    assert client.call_count == 1
    span = client.last_span
    assert span["name"].startswith("tool.")
    attrs = span["attrs"]
    assert attrs["gen_ai.tool.name"] == "reverse"
    assert attrs["strathon.tool.name"] == "reverse"
    # Args are JSON-serialized so CEL string predicates can match
    assert "abc" in attrs["strathon.tool.args"]
    # Framework auto-detected from the tool's class module
    assert attrs.get("strathon.framework") == "langchain"


# ---- Failure isolation ---------------------------------------------------


def test_policy_check_exception_does_not_break_tool():
    """A bug in policy evaluation must never take down the user's app."""
    tool = _make_tool()
    client = _FakeClient(PolicyDecision(action="allow"), raise_on_check=True)
    enforce_steer(tool, client)

    # check_policy will raise, but the tool body still runs
    result = tool.invoke({"text": "hi"})

    assert result == "ih"


# ---- Idempotent enrollment ----------------------------------------------


def test_double_enroll_retargets_client_without_duplicate_patching():
    tool = _make_tool()

    client1 = _FakeClient(PolicyDecision(action="allow"))
    enforce_steer(tool, client1)
    tool.invoke({"text": "first"})
    assert client1.call_count == 1

    # Re-enroll with a different client
    client2 = _FakeClient(PolicyDecision(action="steer", replacement="STEERED"))
    enforce_steer(tool, client2)

    result = tool.invoke({"text": "second"})
    # New client's decision applied
    assert result == "STEERED"
    assert client2.call_count == 1
    # Old client got no new calls
    assert client1.call_count == 1


# ---- Disable -------------------------------------------------------------


def test_disable_steer_returns_tool_to_original_behavior():
    tool = _make_tool()
    client = _FakeClient(PolicyDecision(action="steer", replacement="X"))
    enforce_steer(tool, client)

    assert tool.invoke({"text": "a"}) == "X"

    disable_steer(tool)

    # After disable: tool body runs and no policy check is performed
    result = tool.invoke({"text": "abc"})
    assert result == "cba"
    # check_policy was called once (for the enrolled invocation), not the
    # second post-disable invocation
    assert client.call_count == 1


# ---- Non-enrolled tools of a patched class are unaffected ---------------


def test_non_enrolled_tools_of_a_patched_class_are_unaffected():
    """Class-level patching mustn't make every tool of that class subject
    to the registered client's policies."""
    enrolled_tool = _make_tool()
    not_enrolled_tool = _make_tool()

    client = _FakeClient(PolicyDecision(action="block", message="no"))
    enforce_steer(enrolled_tool, client)

    # Patched class — not_enrolled_tool's invoke goes through the patch
    # too — but its instance is not in the registry, so the patch
    # short-circuits to the original.
    result = not_enrolled_tool.invoke({"text": "x"})
    assert result == "x"
    # The enrolled one still blocks
    with pytest.raises(StrathonPolicyBlocked):
        enrolled_tool.invoke({"text": "x"})


# ---- Bad input -----------------------------------------------------------


def test_enforce_steer_rejects_object_without_invoke():
    class NotATool:
        pass

    with pytest.raises(TypeError, match="invoke"):
        enforce_steer(NotATool(), _FakeClient(PolicyDecision(action="allow")))


# ---- Async path ---------------------------------------------------------


def test_async_steer_returns_replacement_via_ainvoke():
    """The patched ainvoke must honor steer the same way invoke does."""
    import asyncio

    tool = _make_tool()
    client = _FakeClient(
        PolicyDecision(action="steer", replacement="ASYNC_STEERED"),
    )
    enforce_steer(tool, client)

    async def run():
        return await tool.ainvoke({"text": "ignored"})

    result = asyncio.run(run())
    assert result == "ASYNC_STEERED"


def test_async_block_raises_via_ainvoke():
    import asyncio

    tool = _make_tool()
    client = _FakeClient(
        PolicyDecision(action="block", message="async block"),
    )
    enforce_steer(tool, client)

    async def run():
        await tool.ainvoke({"text": "x"})

    with pytest.raises(StrathonPolicyBlocked, match="async block"):
        asyncio.run(run())


def test_async_allow_runs_real_body_via_ainvoke():
    import asyncio

    tool = _make_tool()
    client = _FakeClient(PolicyDecision(action="allow"))
    enforce_steer(tool, client)

    async def run():
        return await tool.ainvoke({"text": "abc"})

    assert asyncio.run(run()) == "cba"

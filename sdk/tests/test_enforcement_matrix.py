"""Enforcement matrix: every action x both dispatchers.

This is the durable guard against the silent-allow defect class. The bugs that
prompted it (require_approval silently allowed on the async ainvoke path and the
OpenAI Agents guardrail, and operator halts not checked on most surfaces) all
hid in the same place: a cell of the {action} x {surface} grid that no test
covered.

Both call-affecting dispatchers — the sync ``dispatch_policy_decision`` and the
async ``dispatch_policy_decision_async`` — are the single engines every
instrumentation surface now routes through. This test asserts that for each
action, each dispatcher applies it (or fails closed) rather than running the
tool body. ``on_allow`` here stands in for "the real tool ran": if it runs when
it shouldn't, that is a silent allow and the test fails.

The per-adapter tests cover the surface-specific wiring; this covers the
invariant itself, exhaustively, so a newly added action or a refactor can't
quietly leave a cell empty again.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from strathon.policy.steer import (
    dispatch_policy_decision,
    dispatch_policy_decision_async,
)
from strathon.policy.types import (
    StrathonApprovalDenied,
    StrathonHaltExceeded,
    StrathonPolicyBlocked,
    StrathonPolicyThrottled,
)

ATTRS = {"strathon.tool.name": "send_email", "gen_ai.tool.name": "send_email"}
SPAN = "tool.send_email"


def _decision(**flags):
    d = MagicMock()
    for f in (
        "is_block", "is_throttle", "is_steer",
        "is_require_approval", "is_allow",
    ):
        setattr(d, f, flags.get(f, False))
    d.message = flags.get("message")
    d.replacement = flags.get("replacement")
    d.policy_id = "pol_1"
    d.policy_name = "test-policy"
    d.retry_after_seconds = flags.get("retry_after_seconds")
    return d


def _client(decision, *, halt=False, approval_granted=True):
    """A client whose check_halt/check_policy return controlled decisions.

    Approval is monkeypatched at call sites that need it (the async dispatcher
    imports await_for_approval lazily), so here we only wire halt + policy.
    """
    client = MagicMock()
    halt_dec = MagicMock()
    halt_dec.is_halt = halt
    halt_dec.halt_id = "halt_1"
    halt_dec.scope = "project"
    halt_dec.scope_value = None
    halt_dec.reason = "stopped"
    client.check_halt.return_value = halt_dec
    client.check_policy.return_value = decision
    client.tracer = MagicMock()
    return client


class _Ran:
    """Tracks whether the 'real tool body' executed."""
    def __init__(self):
        self.ran = False

    def __call__(self):
        self.ran = True
        return "TOOL_RAN"


# --------------------------------------------------------------------------
# Sync dispatcher
# --------------------------------------------------------------------------

class TestSyncDispatcherMatrix:
    def test_block_raises_and_tool_never_runs(self):
        ran = _Ran()
        client = _client(_decision(is_block=True))
        with pytest.raises(StrathonPolicyBlocked):
            dispatch_policy_decision(client, span_name=SPAN, attrs=ATTRS, on_allow=ran)
        assert not ran.ran

    def test_throttle_raises_and_tool_never_runs(self):
        ran = _Ran()
        client = _client(_decision(is_throttle=True))
        with pytest.raises(StrathonPolicyThrottled):
            dispatch_policy_decision(client, span_name=SPAN, attrs=ATTRS, on_allow=ran)
        assert not ran.ran

    def test_steer_returns_replacement_and_tool_never_runs(self):
        ran = _Ran()
        client = _client(_decision(is_steer=True, replacement="REDIRECTED"))
        out = dispatch_policy_decision(client, span_name=SPAN, attrs=ATTRS, on_allow=ran)
        assert out == "REDIRECTED"
        assert not ran.ran

    def test_require_approval_denied_blocks_and_tool_never_runs(self, monkeypatch):
        ran = _Ran()
        client = _client(_decision(is_require_approval=True))

        def _deny(*a, **k):
            raise StrathonApprovalDenied("denied", policy_id="pol_1", status="denied")

        monkeypatch.setattr("strathon.policy.approval.wait_for_approval", _deny)
        with pytest.raises(StrathonApprovalDenied):
            dispatch_policy_decision(client, span_name=SPAN, attrs=ATTRS, on_allow=ran)
        assert not ran.ran

    def test_require_approval_granted_runs_tool(self, monkeypatch):
        ran = _Ran()
        client = _client(_decision(is_require_approval=True))
        monkeypatch.setattr(
            "strathon.policy.approval.wait_for_approval", lambda *a, **k: True
        )
        out = dispatch_policy_decision(client, span_name=SPAN, attrs=ATTRS, on_allow=ran)
        assert ran.ran and out == "TOOL_RAN"

    def test_allow_runs_tool(self):
        ran = _Ran()
        client = _client(_decision(is_allow=True))
        out = dispatch_policy_decision(client, span_name=SPAN, attrs=ATTRS, on_allow=ran)
        assert ran.ran and out == "TOOL_RAN"

    def test_halt_raises_before_policy_and_tool_never_runs(self):
        ran = _Ran()
        client = _client(_decision(is_allow=True), halt=True)
        with pytest.raises(StrathonHaltExceeded):
            dispatch_policy_decision(client, span_name=SPAN, attrs=ATTRS, on_allow=ran)
        assert not ran.ran


# --------------------------------------------------------------------------
# Async dispatcher (the surface class where silent-allow gaps had hidden)
# --------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


class TestAsyncDispatcherMatrix:
    def test_block_raises_and_tool_never_runs(self):
        ran = _Ran()
        client = _client(_decision(is_block=True))
        with pytest.raises(StrathonPolicyBlocked):
            _run(dispatch_policy_decision_async(
                client, span_name=SPAN, attrs=ATTRS, on_allow=ran))
        assert not ran.ran

    def test_throttle_raises_and_tool_never_runs(self):
        ran = _Ran()
        client = _client(_decision(is_throttle=True))
        with pytest.raises(StrathonPolicyThrottled):
            _run(dispatch_policy_decision_async(
                client, span_name=SPAN, attrs=ATTRS, on_allow=ran))
        assert not ran.ran

    def test_steer_returns_replacement_and_tool_never_runs(self):
        ran = _Ran()
        client = _client(_decision(is_steer=True, replacement="REDIRECTED"))
        out = _run(dispatch_policy_decision_async(
            client, span_name=SPAN, attrs=ATTRS, on_allow=ran))
        assert out == "REDIRECTED"
        assert not ran.ran

    def test_require_approval_denied_blocks_and_tool_never_runs(self, monkeypatch):
        # The cell that matters most: require_approval on an async surface must
        # NOT silently run the tool. This is the gap that previously went
        # uncovered on the async paths.
        ran = _Ran()
        client = _client(_decision(is_require_approval=True))

        async def _deny(*a, **k):
            raise StrathonApprovalDenied("denied", policy_id="pol_1", status="denied")

        monkeypatch.setattr("strathon.policy.approval.await_for_approval", _deny)
        with pytest.raises(StrathonApprovalDenied):
            _run(dispatch_policy_decision_async(
                client, span_name=SPAN, attrs=ATTRS, on_allow=ran))
        assert not ran.ran

    def test_require_approval_granted_runs_tool(self, monkeypatch):
        ran = _Ran()
        client = _client(_decision(is_require_approval=True))

        async def _grant(*a, **k):
            return True

        monkeypatch.setattr("strathon.policy.approval.await_for_approval", _grant)
        out = _run(dispatch_policy_decision_async(
            client, span_name=SPAN, attrs=ATTRS, on_allow=ran))
        assert ran.ran and out == "TOOL_RAN"

    def test_allow_runs_tool(self):
        ran = _Ran()
        client = _client(_decision(is_allow=True))
        out = _run(dispatch_policy_decision_async(
            client, span_name=SPAN, attrs=ATTRS, on_allow=ran))
        assert ran.ran and out == "TOOL_RAN"

    def test_halt_raises_before_policy_and_tool_never_runs(self):
        # Halt must be checked on the async path too, not only the sync one.
        ran = _Ran()
        client = _client(_decision(is_allow=True), halt=True)
        with pytest.raises(StrathonHaltExceeded):
            _run(dispatch_policy_decision_async(
                client, span_name=SPAN, attrs=ATTRS, on_allow=ran))
        assert not ran.ran

    def test_async_on_allow_coroutine_is_awaited(self):
        # The async dispatcher must await an async tool body, not return the
        # unawaited coroutine.
        client = _client(_decision(is_allow=True))
        marker = {}

        async def _async_body():
            marker["ran"] = True
            return "ASYNC_TOOL_RAN"

        out = _run(dispatch_policy_decision_async(
            client, span_name=SPAN, attrs=ATTRS, on_allow=_async_body))
        assert marker.get("ran") and out == "ASYNC_TOOL_RAN"


class TestUnrecognizedDecisionFailsClosed:
    """Forward-compat: a decision that is neither allow nor a known
    call-affecting action must fail closed, not run the tool."""

    def test_sync_unknown_action_blocks(self):
        ran = _Ran()
        # All flags False => not allow, not block/throttle/steer/approval.
        client = _client(_decision())
        with pytest.raises(StrathonPolicyBlocked):
            dispatch_policy_decision(client, span_name=SPAN, attrs=ATTRS, on_allow=ran)
        assert not ran.ran

    def test_async_unknown_action_blocks(self):
        ran = _Ran()
        client = _client(_decision())
        with pytest.raises(StrathonPolicyBlocked):
            _run(dispatch_policy_decision_async(
                client, span_name=SPAN, attrs=ATTRS, on_allow=ran))
        assert not ran.ran

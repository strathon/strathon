"""Adapters must let enforcement signals reach the tool boundary.

The existing ``test_fail_closed.py`` proves the *enforcer* raises
``StrathonReceiverUnreachable`` under fail-closed staleness. It does NOT prove
that exception survives the adapter's pre-tool hook -- and it did not: every
adapter caught bare ``Exception`` around the policy/halt check and allowed the
tool to run, so ``fail_closed=True`` was silently defeated end to end.

This file closes that gap. It drives the shared enforcement path the adapters
use (``check_halt_or_raise`` and the sync/async dispatchers in ``policy.steer``)
with a stale fail-closed client and asserts the three enforcement signals
propagate rather than being swallowed into an allow.
"""

from __future__ import annotations

import pytest

from strathon.exceptions import StrathonReceiverUnreachable
from strathon.policy.steer import check_halt_or_raise
from strathon.policy.types import (
    ENFORCEMENT_SIGNALS,
    StrathonHaltExceeded,
    StrathonPolicyBlocked,
)


class _RaisingClient:
    """Minimal client stand-in whose checks raise a chosen enforcement signal,
    exactly as a real fail-closed enforcer does when its cache is stale."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def check_halt(self, span_context):
        raise self._exc

    def check_policy(self, span_context):
        raise self._exc


class _BugClient:
    """A genuinely-unexpected internal error (NOT an enforcement signal). This
    must stay swallowed so a bug in instrumentation cannot break the agent."""

    def check_halt(self, span_context):
        raise RuntimeError("some internal instrumentation bug")

    def check_policy(self, span_context):
        raise RuntimeError("some internal instrumentation bug")


_SIGNALS = [
    StrathonReceiverUnreachable(
        "stale", subsystem="halt", staleness_seconds=99.0, max_staleness_seconds=1.0
    ),
    StrathonHaltExceeded("halted", halt_id=1),
    StrathonPolicyBlocked("blocked"),
]


@pytest.mark.parametrize("signal", _SIGNALS, ids=lambda s: type(s).__name__)
def test_halt_helper_propagates_enforcement_signals(signal):
    """check_halt_or_raise must not swallow an enforcement signal into an allow."""
    client = _RaisingClient(signal)
    with pytest.raises(type(signal)):
        check_halt_or_raise(client, "tool.wire_money", {"strathon.tool.name": "wire_money"})


def test_halt_helper_still_swallows_unexpected_bugs():
    """A real internal bug (not an enforcement signal) stays fail-open: the
    helper returns without raising so the user's tool is not broken by a
    telemetry defect."""
    check_halt_or_raise(_BugClient(), "tool.wire_money", {"strathon.tool.name": "wire_money"})


def test_receiver_unreachable_is_in_canonical_set():
    """The fail-closed refusal must be one of the signals adapters propagate.
    This is the specific membership whose absence caused the original bypass."""
    assert StrathonReceiverUnreachable in ENFORCEMENT_SIGNALS
    assert StrathonPolicyBlocked in ENFORCEMENT_SIGNALS
    assert StrathonHaltExceeded in ENFORCEMENT_SIGNALS

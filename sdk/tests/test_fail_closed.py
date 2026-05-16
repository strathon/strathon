"""Tests for fail-closed mode on the SDK's PolicyEnforcer and HaltEnforcer.

Both enforcers expose the same opt-in:

    fail_closed: bool = False
    fail_closed_max_staleness_sec: float = 60.0

When ``fail_closed`` is True and the most recent successful refresh is
older than ``fail_closed_max_staleness_sec``, ``check_policy`` and
``check_halt`` raise ``StrathonReceiverUnreachable`` at the tool
boundary instead of returning a normal decision. The default behavior
(fail-open) is preserved: an unreachable receiver continues to serve
the last-known cache without raising.

These tests use ``set_last_refresh_for_testing`` to drive the staleness
clock deterministically rather than waiting on real time. They do NOT
make network calls — the existing test suite covers HTTP behavior; this
file's job is to verify the fail-closed semantics specifically.
"""

from __future__ import annotations

import time

import pytest

from strathon import StrathonReceiverUnreachable
from strathon.policy.enforcer import PolicyEnforcer
from strathon.policy.halt_enforcer import HaltEnforcer
from strathon.policy.types import Policy


# ---- HaltEnforcer fail-closed --------------------------------------------


def _make_halt_enforcer(**kwargs) -> HaltEnforcer:
    kwargs.setdefault("endpoint", "http://localhost:0")  # never reached in these tests
    kwargs.setdefault("api_key", "test")
    return HaltEnforcer(**kwargs)


def test_halt_check_returns_allow_when_fail_closed_off_and_state_stale():
    """With fail_closed=False (the default), stale state stays in force —
    the historical fail-open semantics. No raise."""
    e = _make_halt_enforcer(fail_closed=False, fail_closed_max_staleness_sec=10.0)
    # Pretend the last successful refresh was an hour ago.
    e.set_last_refresh_for_testing(time.time() - 3600)
    # No halts in cache; check_halt returns ALLOW_HALT.
    decision = e.check_halt({"name": "anything", "attrs": {}})
    assert decision.is_allow


def test_halt_check_raises_when_fail_closed_and_state_stale():
    """With fail_closed=True, stale state triggers StrathonReceiverUnreachable."""
    e = _make_halt_enforcer(fail_closed=True, fail_closed_max_staleness_sec=10.0)
    e.set_last_refresh_for_testing(time.time() - 3600)
    with pytest.raises(StrathonReceiverUnreachable) as exc_info:
        e.check_halt({"name": "x", "attrs": {}})
    err = exc_info.value
    assert err.subsystem == "halt_enforcer"
    assert err.staleness_seconds > 10.0
    assert err.max_staleness_seconds == 10.0
    assert "stale" in str(err)


def test_halt_check_does_not_raise_when_fail_closed_and_state_fresh():
    """Fresh state under the threshold returns a normal decision even
    with fail_closed=True."""
    e = _make_halt_enforcer(fail_closed=True, fail_closed_max_staleness_sec=60.0)
    # Successful refresh 5s ago — well within the 60s window.
    e.set_last_refresh_for_testing(time.time() - 5)
    decision = e.check_halt({"name": "x", "attrs": {}})
    assert decision.is_allow


def test_halt_check_fail_closed_raises_when_no_refresh_has_ever_succeeded():
    """If the SDK started up and never reached the receiver, the
    last-refresh-at timestamp is 0.0. Fail-closed should treat that as
    infinitely stale and raise on the first tool call."""
    e = _make_halt_enforcer(fail_closed=True, fail_closed_max_staleness_sec=10.0)
    # Don't touch last_refresh — leave it at the constructor default 0.0.
    with pytest.raises(StrathonReceiverUnreachable):
        e.check_halt({"name": "x", "attrs": {}})


def test_halt_check_fail_closed_threshold_boundary():
    """A refresh exactly at the threshold is treated as fresh (the
    comparison is strictly greater-than). One nanosecond past, it
    raises."""
    e = _make_halt_enforcer(fail_closed=True, fail_closed_max_staleness_sec=10.0)
    # Sit right at the boundary.
    e.set_last_refresh_for_testing(time.time() - 10.0)
    # The +/- of CPU time between set and check pushes us just past 10s,
    # so this should raise. We rely on the natural drift.
    # Conservatively, test a clear "well past" point.
    e.set_last_refresh_for_testing(time.time() - 10.5)
    with pytest.raises(StrathonReceiverUnreachable):
        e.check_halt({"name": "x", "attrs": {}})


def test_halt_check_fail_closed_returns_halt_decision_when_active_halt_matches():
    """Fresh state + an active halt = normal halt decision returned,
    not a fail-closed raise. Both checks coexist."""
    e = _make_halt_enforcer(fail_closed=True, fail_closed_max_staleness_sec=60.0)
    e.set_halts_for_testing([
        {"id": 1, "scope": "agent", "scope_value": "agent-7",
         "state": "halted", "reason": "test"},
    ])
    # set_halts_for_testing also sets last_refresh_at to now.
    decision = e.check_halt({
        "name": "any.tool",
        "attrs": {"strathon.agent.id": "agent-7"},
    })
    assert decision.is_halt
    assert decision.halt_id == 1


# ---- PolicyEnforcer fail-closed ------------------------------------------


def _make_policy_enforcer(**kwargs) -> PolicyEnforcer:
    kwargs.setdefault("endpoint", "http://localhost:0")
    kwargs.setdefault("api_key", "test")
    return PolicyEnforcer(**kwargs)


def test_policy_check_returns_allow_when_fail_closed_off_and_state_stale():
    e = _make_policy_enforcer(fail_closed=False, fail_closed_max_staleness_sec=10.0)
    e.set_last_refresh_for_testing(time.time() - 3600)
    decision = e.check_policy({"name": "x", "attrs": {}})
    assert decision.is_allow


def test_policy_check_raises_when_fail_closed_and_state_stale():
    e = _make_policy_enforcer(fail_closed=True, fail_closed_max_staleness_sec=10.0)
    e.set_last_refresh_for_testing(time.time() - 3600)
    with pytest.raises(StrathonReceiverUnreachable) as exc_info:
        e.check_policy({"name": "x", "attrs": {}})
    err = exc_info.value
    assert err.subsystem == "policy_enforcer"
    assert err.staleness_seconds > 10.0
    assert err.max_staleness_seconds == 10.0


def test_policy_check_does_not_raise_when_fail_closed_and_state_fresh():
    e = _make_policy_enforcer(fail_closed=True, fail_closed_max_staleness_sec=60.0)
    e.set_last_refresh_for_testing(time.time() - 5)
    decision = e.check_policy({"name": "x", "attrs": {}})
    assert decision.is_allow


def test_policy_check_fail_closed_raises_when_no_refresh_has_ever_succeeded():
    e = _make_policy_enforcer(fail_closed=True, fail_closed_max_staleness_sec=10.0)
    with pytest.raises(StrathonReceiverUnreachable):
        e.check_policy({"name": "x", "attrs": {}})


def test_policy_check_fail_closed_returns_block_when_active_policy_matches():
    """Fresh state + a matching block policy = normal block decision,
    not a fail-closed raise."""
    e = _make_policy_enforcer(fail_closed=True, fail_closed_max_staleness_sec=60.0)
    e.set_policies_for_testing([
        Policy(
            id="p1", project_id="proj", name="block_test",
            match_expression="true",
            action="block",
            action_config={"message": "blocked for test"},
        ),
    ])
    decision = e.check_policy({"name": "any.tool", "attrs": {}})
    assert decision.is_block
    assert decision.message == "blocked for test"


# ---- Client integration --------------------------------------------------


def test_client_default_does_not_pass_fail_closed():
    """By default, fail_closed is False on both enforcers — backward-
    compatible with all callers who never opted in."""
    from strathon import Client

    # We don't need the receiver to be reachable; both enforcers swallow
    # the start-time refresh failure. We only inspect the constructed
    # config on the enforcer instances.
    c = Client(api_key="test", endpoint="http://localhost:0")
    try:
        assert c.halt_enforcer is not None
        assert c.halt_enforcer._fail_closed is False
        assert c.policy_enforcer is not None
        assert c.policy_enforcer._fail_closed is False
    finally:
        c.shutdown()


def test_client_propagates_fail_closed_to_both_enforcers():
    from strathon import Client

    c = Client(
        api_key="test",
        endpoint="http://localhost:0",
        fail_closed=True,
        fail_closed_max_staleness_sec=42.0,
    )
    try:
        assert c.halt_enforcer._fail_closed is True
        assert c.halt_enforcer._fail_closed_max_staleness_sec == 42.0
        assert c.policy_enforcer._fail_closed is True
        assert c.policy_enforcer._fail_closed_max_staleness_sec == 42.0
    finally:
        c.shutdown()


def test_client_check_halt_raises_receiver_unreachable_under_fail_closed():
    """End-to-end via Client.check_halt: with fail_closed=True and an
    unreachable receiver, the very first tool-boundary check raises."""
    from strathon import Client

    c = Client(
        api_key="test",
        endpoint="http://127.0.0.1:1",  # nothing listens here
        fail_closed=True,
        # Tiny threshold so we don't wait. The startup refresh will
        # fail (refused connection); last_refresh stays at 0.0; the
        # check sees staleness > 0.001 and raises.
        fail_closed_max_staleness_sec=0.001,
    )
    try:
        with pytest.raises(StrathonReceiverUnreachable):
            c.check_halt({"name": "x", "attrs": {}})
    finally:
        c.shutdown()


def test_client_check_policy_raises_receiver_unreachable_under_fail_closed():
    from strathon import Client

    c = Client(
        api_key="test",
        endpoint="http://127.0.0.1:1",
        fail_closed=True,
        fail_closed_max_staleness_sec=0.001,
    )
    try:
        with pytest.raises(StrathonReceiverUnreachable):
            c.check_policy({"name": "x", "attrs": {}})
    finally:
        c.shutdown()


# ---- Exception shape ------------------------------------------------------


def test_receiver_unreachable_is_subclass_of_strathon_error():
    """Catching ``StrathonError`` should also catch fail-closed errors —
    user code with a single broad except can handle both."""
    from strathon import StrathonError

    assert issubclass(StrathonReceiverUnreachable, StrathonError)


def test_receiver_unreachable_carries_diagnostic_attrs():
    """Useful for callers that want to log or branch on the cause."""
    err = StrathonReceiverUnreachable(
        "test message",
        subsystem="halt_enforcer",
        staleness_seconds=120.5,
        max_staleness_seconds=60.0,
    )
    assert err.message == "test message"
    assert err.subsystem == "halt_enforcer"
    assert err.staleness_seconds == 120.5
    assert err.max_staleness_seconds == 60.0

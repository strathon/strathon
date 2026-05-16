"""Tests for the throttle policy action.

Two layers:

* Token-bucket / ThrottleStore unit tests — pure mechanics.
* PolicyEnforcer integration — drive ``check_policy`` with seeded
  throttle policies and verify the decision shape, scope-key
  isolation, malformed-config tolerance, and time-based refill.
"""

from __future__ import annotations

import pytest

from strathon import StrathonPolicyBlocked, StrathonPolicyThrottled
from strathon.policy.enforcer import PolicyEnforcer
from strathon.policy.throttle import (
    IDLE_TIMEOUT_SECONDS,
    PRUNE_INTERVAL_SECONDS,
    ThrottleStore,
)
from strathon.policy.types import Policy


# ---- ThrottleStore mechanics ---------------------------------------------


def test_throttle_store_starts_full_and_admits_burst():
    """A new bucket lets ``max_calls`` consecutive calls through without
    any time elapsing."""
    s = ThrottleStore()
    for _ in range(5):
        allowed, retry_after = s.consume(
            policy_id="p1", scope_key="agent-a",
            max_calls=5, window_seconds=10.0,
            now=0.0,
        )
        assert allowed is True
        assert retry_after == 0.0


def test_throttle_store_denies_when_bucket_empty():
    """After exhausting the burst, the next call is denied with a
    retry-after derived from the refill rate."""
    s = ThrottleStore()
    for _ in range(5):
        s.consume(
            policy_id="p1", scope_key="agent-a",
            max_calls=5, window_seconds=10.0,
            now=0.0,
        )
    allowed, retry_after = s.consume(
        policy_id="p1", scope_key="agent-a",
        max_calls=5, window_seconds=10.0,
        now=0.0,
    )
    assert allowed is False
    # 5 calls / 10s = 0.5 tokens/s, so one token takes 2s.
    assert retry_after == pytest.approx(2.0, abs=0.01)


def test_throttle_store_refills_over_time():
    """After enough elapsed time, the bucket recovers tokens at the
    configured rate."""
    s = ThrottleStore()
    # Drain.
    for _ in range(3):
        s.consume(
            policy_id="p1", scope_key="agent-a",
            max_calls=3, window_seconds=6.0,
            now=0.0,
        )
    # 4s later: 4s * (3/6) = 2 tokens refilled, capped at capacity 3.
    allowed, _ = s.consume(
        policy_id="p1", scope_key="agent-a",
        max_calls=3, window_seconds=6.0,
        now=4.0,
    )
    assert allowed is True


def test_throttle_store_isolates_by_policy_id():
    """Two policies with the same scope_key do not share a bucket."""
    s = ThrottleStore()
    # Drain policy p1's bucket for agent-a.
    s.consume(policy_id="p1", scope_key="agent-a",
              max_calls=1, window_seconds=60.0, now=0.0)
    allowed_p1, _ = s.consume(policy_id="p1", scope_key="agent-a",
                              max_calls=1, window_seconds=60.0, now=0.0)
    assert allowed_p1 is False
    # Policy p2 has its own bucket; the same agent gets a fresh start.
    allowed_p2, _ = s.consume(policy_id="p2", scope_key="agent-a",
                              max_calls=1, window_seconds=60.0, now=0.0)
    assert allowed_p2 is True


def test_throttle_store_isolates_by_scope_key():
    """Two scope_keys for the same policy do not share a bucket."""
    s = ThrottleStore()
    s.consume(policy_id="p1", scope_key="agent-a",
              max_calls=1, window_seconds=60.0, now=0.0)
    a_denied, _ = s.consume(policy_id="p1", scope_key="agent-a",
                            max_calls=1, window_seconds=60.0, now=0.0)
    assert a_denied is False
    b_allowed, _ = s.consume(policy_id="p1", scope_key="agent-b",
                             max_calls=1, window_seconds=60.0, now=0.0)
    assert b_allowed is True


def test_throttle_store_live_config_change_applies_on_next_consume():
    """Raising max_calls or changing window_seconds takes effect on the
    next consume call — no need to rebuild the bucket."""
    s = ThrottleStore()
    # Drain a 1-token-capacity bucket.
    s.consume(policy_id="p1", scope_key="a",
              max_calls=1, window_seconds=10.0, now=0.0)
    denied, _ = s.consume(policy_id="p1", scope_key="a",
                          max_calls=1, window_seconds=10.0, now=0.0)
    assert denied is False
    # Operator raises the cap. Bucket still has 0 tokens, but refill
    # rate is higher now; after 1s, 1 token / 1s = 1 token refilled.
    allowed, _ = s.consume(policy_id="p1", scope_key="a",
                           max_calls=10, window_seconds=10.0, now=1.0)
    assert allowed is True


def test_throttle_store_prunes_idle_buckets():
    """Buckets idle longer than IDLE_TIMEOUT_SECONDS get dropped on the
    next sweep so a fleet of short-lived agent ids doesn't leak."""
    s = ThrottleStore()
    s.consume(policy_id="p1", scope_key="ephemeral",
              max_calls=1, window_seconds=60.0, now=0.0)
    assert s.num_buckets == 1
    future = max(PRUNE_INTERVAL_SECONDS, IDLE_TIMEOUT_SECONDS) + 10.0
    s.consume(policy_id="p1", scope_key="fresh",
              max_calls=1, window_seconds=60.0, now=future)
    # ephemeral pruned, fresh added — net 1.
    assert s.num_buckets == 1


# ---- PolicyEnforcer integration ------------------------------------------


def _enforcer() -> PolicyEnforcer:
    return PolicyEnforcer(endpoint="http://localhost:0", api_key="test")


def _throttle_policy(
    *,
    policy_id: str = "p-throttle",
    name: str = "throttle_test",
    match_expression: str = "true",
    max_calls: int = 2,
    window_seconds: float = 60.0,
    scope: str = "agent",
    priority: int = 0,
) -> Policy:
    return Policy(
        id=policy_id,
        project_id="proj",
        name=name,
        match_expression=match_expression,
        action="throttle",
        action_config={
            "max_calls": max_calls,
            "window_seconds": window_seconds,
            "scope": scope,
        },
        priority=priority,
    )


def _ctx(agent_id: str = "agent-x", name: str = "any.tool") -> dict:
    return {"name": name, "attrs": {"strathon.agent.id": agent_id}}


def test_throttle_policy_admits_calls_under_the_cap():
    """When the bucket has tokens, throttle decisions are NOT returned —
    a downstream lower-priority block can still match, and an
    unconditional allow is the default."""
    e = _enforcer()
    e.set_policies_for_testing([
        _throttle_policy(max_calls=3, window_seconds=60.0),
    ])
    # First 3 calls all admitted -> ALLOW (no throttle decision).
    for _ in range(3):
        d = e.check_policy(_ctx())
        assert d.is_allow, f"expected allow, got {d.action}"


def test_throttle_policy_returns_throttle_decision_when_exhausted():
    e = _enforcer()
    e.set_policies_for_testing([
        _throttle_policy(max_calls=2, window_seconds=60.0),
    ])
    # Drain.
    assert e.check_policy(_ctx()).is_allow
    assert e.check_policy(_ctx()).is_allow
    # Next call: throttled.
    d = e.check_policy(_ctx())
    assert d.is_throttle
    assert d.policy_name == "throttle_test"
    assert d.message is not None
    assert d.retry_after_seconds is not None and d.retry_after_seconds > 0


def test_throttle_scope_agent_isolates_per_agent():
    """Default scope=agent: agent-a being throttled doesn't affect agent-b."""
    e = _enforcer()
    e.set_policies_for_testing([
        _throttle_policy(max_calls=1, window_seconds=60.0, scope="agent"),
    ])
    assert e.check_policy(_ctx(agent_id="agent-a")).is_allow
    # agent-a now exhausted.
    assert e.check_policy(_ctx(agent_id="agent-a")).is_throttle
    # agent-b has its own bucket.
    assert e.check_policy(_ctx(agent_id="agent-b")).is_allow


def test_throttle_scope_global_shares_bucket_across_agents():
    """scope=global: all agents draw from one bucket per policy."""
    e = _enforcer()
    e.set_policies_for_testing([
        _throttle_policy(max_calls=1, window_seconds=60.0, scope="global"),
    ])
    assert e.check_policy(_ctx(agent_id="agent-a")).is_allow
    # Bucket exhausted; agent-b is throttled even though it's a different
    # agent — global scope.
    assert e.check_policy(_ctx(agent_id="agent-b")).is_throttle


def test_throttle_does_not_block_a_later_higher_priority_block():
    """When a throttle policy admits the call but a higher-priority
    block rule also matches, the block wins. Verified by setting up:
    - higher-priority block policy
    - lower-priority throttle policy
    The block should always fire even when the throttle bucket has tokens.
    """
    e = _enforcer()
    e.set_policies_for_testing([
        Policy(
            id="block", project_id="proj", name="hard_block",
            match_expression="true", action="block",
            action_config={"message": "hard block wins"},
            priority=100,
        ),
        _throttle_policy(max_calls=10, window_seconds=60.0, priority=1),
    ])
    d = e.check_policy(_ctx())
    assert d.is_block
    assert d.message == "hard block wins"


def test_throttle_admitted_call_does_not_short_circuit_lower_priority_block():
    """A throttle policy at HIGHER priority that admits the call must
    NOT prevent a lower-priority block from being evaluated. (Throttle
    that admits is essentially 'no opinion on this call'.)"""
    e = _enforcer()
    e.set_policies_for_testing([
        _throttle_policy(max_calls=10, window_seconds=60.0, priority=100),
        Policy(
            id="block", project_id="proj", name="hard_block",
            match_expression="true", action="block",
            action_config={"message": "block fires even after throttle admits"},
            priority=1,
        ),
    ])
    d = e.check_policy(_ctx())
    assert d.is_block


def test_throttle_decision_carries_retry_after_consistent_with_config():
    """retry_after_seconds should reflect (1 token) / (max_calls/window)."""
    e = _enforcer()
    e.set_policies_for_testing([
        _throttle_policy(max_calls=4, window_seconds=8.0),
    ])
    # Drain.
    for _ in range(4):
        e.check_policy(_ctx())
    d = e.check_policy(_ctx())
    assert d.is_throttle
    # 4 calls per 8s = 0.5 tokens/s. One token = 2s.
    assert d.retry_after_seconds == pytest.approx(2.0, abs=0.1)


def test_throttle_malformed_config_admits_call_with_warning(caplog):
    """A throttle policy whose config is missing required keys must
    NOT silently block agents. Log a warning and admit the call."""
    e = _enforcer()
    e.set_policies_for_testing([
        Policy(
            id="bad", project_id="proj", name="bad_throttle",
            match_expression="true", action="throttle",
            action_config={"max_calls": "not-an-int"},  # malformed
        ),
    ])
    with caplog.at_level("WARNING"):
        d = e.check_policy(_ctx())
    assert d.is_allow
    assert any("malformed action_config" in m for m in caplog.messages)


def test_throttle_dispatch_raises_strathon_policy_throttled():
    """End-to-end through dispatch_policy_decision: a throttled
    decision raises StrathonPolicyThrottled, and that exception is a
    subclass of StrathonPolicyBlocked."""
    from strathon.policy.steer import dispatch_policy_decision

    e = _enforcer()
    e.set_policies_for_testing([
        _throttle_policy(max_calls=1, window_seconds=60.0),
    ])

    # Minimal client stand-in: needs check_policy + a tracer for the
    # intervention span. We bypass span emission by patching it to a
    # no-op; the test focuses on the raise behavior.
    class _FakeClient:
        def __init__(self, enforcer):
            self._enforcer = enforcer

        def check_policy(self, ctx):
            return self._enforcer.check_policy(ctx)

        @property
        def tracer(self):
            return None  # _emit_intervention_span handles a None tracer

    client = _FakeClient(e)

    # First call: admitted. on_allow runs.
    called = []
    result = dispatch_policy_decision(
        client,
        span_name="tool.t",
        attrs={"strathon.tool.name": "t", "strathon.agent.id": "agent-a"},
        on_allow=lambda: called.append("ran") or "ok",
    )
    assert result == "ok"
    assert called == ["ran"]

    # Second call: throttled. Must raise.
    with pytest.raises(StrathonPolicyThrottled) as exc_info:
        dispatch_policy_decision(
            client,
            span_name="tool.t",
            attrs={"strathon.tool.name": "t", "strathon.agent.id": "agent-a"},
            on_allow=lambda: "should not run",
        )
    err = exc_info.value
    assert err.policy_name == "throttle_test"
    assert err.retry_after_seconds is not None
    # Subclass relationship preserved for backward-compat catches.
    assert isinstance(err, StrathonPolicyBlocked)


def test_strathon_policy_throttled_is_subclass_of_blocked():
    assert issubclass(StrathonPolicyThrottled, StrathonPolicyBlocked)


def test_strathon_policy_throttled_constructor_attrs():
    err = StrathonPolicyThrottled(
        "msg", policy_id="p1", policy_name="t", retry_after_seconds=4.2,
    )
    assert err.message == "msg"
    assert err.policy_id == "p1"
    assert err.policy_name == "t"
    assert err.retry_after_seconds == 4.2

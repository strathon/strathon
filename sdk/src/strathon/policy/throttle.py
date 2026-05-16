"""Token-bucket rate limiter used by ``action: "throttle"`` policies.

Design
======

A throttle policy carries an ``action_config`` of shape::

    {
        "max_calls":      <int>,
        "window_seconds": <number>,
        "scope":          "agent" | "global"   # default "agent"
    }

When the policy's CEL match expression fires at the tool boundary, the
``PolicyEnforcer`` looks up a token bucket keyed by ``(policy_id,
scope_key)`` and tries to consume one token. The scope_key is the
agent id (or "global" for shared buckets). If the bucket can consume,
the call proceeds; if not, the decision becomes ``throttle`` and the
framework integration raises :class:`StrathonPolicyThrottled`.

Why a token bucket rather than a sliding window
-----------------------------------------------

A sliding-window counter is more accurate over short windows but
requires per-call timestamps and either O(N) sweep or a circular
buffer. A token bucket gives the same "average ``max_calls`` per
``window_seconds``" guarantee with O(1) work and one float of state,
which is the right trade for an in-process limiter that runs on every
tool boundary.

Thread safety
-------------

The SDK's enforcement layer runs on whatever thread the host framework
provides — synchronous for most frameworks, an event-loop thread for
async ones. We use :class:`threading.RLock` rather than
``asyncio.Lock`` because (a) the existing PolicyEnforcer and
HaltEnforcer already use RLock, and (b) async hooks call into us
synchronously from the framework's await point, so an asyncio lock
would deadlock if the same coroutine recursed (it won't, but RLock
matches the surrounding code's posture).

The receiver has a parallel in-memory token bucket implementation
under ``receiver/rate_limit.py`` that uses ``asyncio.Lock``. The two
are kept independent on purpose: the SDK is Apache 2.0 and the
receiver is MIT, the lock primitive differs, and dragging a shared
package into both would force every SDK install to ship a dependency
graph it doesn't need.

Idle-bucket pruning
-------------------

The ``ThrottleStore`` allocates one bucket per ``(policy_id,
scope_key)`` pair on first observation. Long-running deployments that
see many distinct agent ids would otherwise leak memory. The store
periodically sweeps buckets idle longer than
:data:`IDLE_TIMEOUT_SECONDS` (sweep cadence
:data:`PRUNE_INTERVAL_SECONDS`). After expiry the next request from
that key gets a full fresh bucket — slightly more permissive than
perfectly tracking history, but the alternative is an unbounded dict.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)


PRUNE_INTERVAL_SECONDS = 60.0
IDLE_TIMEOUT_SECONDS = 300.0


@dataclass
class _Bucket:
    """One token bucket. Mutated only under ``_lock``."""

    capacity: float
    refill_per_second: float
    tokens: float
    last_refill_at: float
    last_used_at: float
    lock: threading.RLock


def _new_bucket(*, capacity: float, refill_per_second: float, now: float) -> _Bucket:
    return _Bucket(
        capacity=capacity,
        refill_per_second=refill_per_second,
        tokens=capacity,  # start full — first call after policy install always allowed
        last_refill_at=now,
        last_used_at=now,
        lock=threading.RLock(),
    )


def _consume_one(bucket: _Bucket, now: float) -> tuple[bool, float]:
    """Try to consume one token from ``bucket``.

    Returns ``(allowed, retry_after_seconds)``. ``retry_after_seconds``
    is 0 on success; on rejection it's the time until the bucket would
    next have at least one token, based on the configured refill rate.
    """
    with bucket.lock:
        elapsed = max(0.0, now - bucket.last_refill_at)
        bucket.tokens = min(
            bucket.capacity, bucket.tokens + elapsed * bucket.refill_per_second,
        )
        bucket.last_refill_at = now
        bucket.last_used_at = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, 0.0

        # Deficit-based retry-after.
        deficit = 1.0 - bucket.tokens
        if bucket.refill_per_second <= 0:
            return False, float("inf")
        return False, deficit / bucket.refill_per_second


class ThrottleStore:
    """Per-policy bucket pool for the SDK's throttle action.

    Buckets are keyed by ``(policy_id, scope_key)`` and lazily created on
    first observation. Each call site looks up its bucket, tries to
    consume one token, and proceeds or yields a retry-after.
    """

    def __init__(self) -> None:
        # The outer lock guards dict mutation when two threads observe a
        # brand-new key concurrently. The hot path acquires only the
        # per-bucket lock so distinct keys don't serialize.
        self._creation_lock = threading.RLock()
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._last_prune_at: float = 0.0

    def consume(
        self,
        *,
        policy_id: str,
        scope_key: str,
        max_calls: int,
        window_seconds: float,
        now: Optional[float] = None,
    ) -> tuple[bool, float]:
        """Try to consume one token from the bucket for ``(policy_id,
        scope_key)``.

        ``max_calls`` and ``window_seconds`` come from the policy's
        ``action_config``. The bucket's capacity is ``max_calls`` and its
        refill rate is ``max_calls / window_seconds`` tokens/second. We
        re-read these on every call so an in-place policy edit
        (operator raises ``max_calls`` from 10 to 100) takes effect on
        the next refresh cycle without having to rebuild the bucket.
        """
        if now is None:
            now = time.monotonic()

        key = (policy_id, scope_key)
        bucket = self._buckets.get(key)

        capacity = float(max_calls)
        refill_per_second = capacity / float(window_seconds) if window_seconds > 0 else 0.0

        if bucket is None:
            with self._creation_lock:
                bucket = self._buckets.get(key)
                if bucket is None:
                    bucket = _new_bucket(
                        capacity=capacity,
                        refill_per_second=refill_per_second,
                        now=now,
                    )
                    self._buckets[key] = bucket
        else:
            # Live config update: an operator may have changed max_calls
            # or window_seconds. Apply the new shape next time the
            # bucket refills. We do NOT clamp existing tokens above the
            # new capacity if the operator lowered max_calls — the next
            # consume will refill toward the (lower) cap naturally.
            with bucket.lock:
                bucket.capacity = capacity
                bucket.refill_per_second = refill_per_second

        result = _consume_one(bucket, now)

        if now - self._last_prune_at >= PRUNE_INTERVAL_SECONDS:
            self._prune_idle_buckets(now)

        return result

    def _prune_idle_buckets(self, now: float) -> None:
        """Drop buckets idle longer than :data:`IDLE_TIMEOUT_SECONDS`."""
        with self._creation_lock:
            self._last_prune_at = now
            stale = [
                key for key, b in self._buckets.items()
                if now - b.last_used_at > IDLE_TIMEOUT_SECONDS
            ]
            for key in stale:
                self._buckets.pop(key, None)

    # ---- Introspection (mainly tests) ----

    @property
    def num_buckets(self) -> int:
        return len(self._buckets)


__all__ = [
    "IDLE_TIMEOUT_SECONDS",
    "PRUNE_INTERVAL_SECONDS",
    "ThrottleStore",
]

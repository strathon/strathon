"""Token-bucket rate limiter, in-memory.

Design
======

The hot path of any request to a rate-limited endpoint goes through
:meth:`RateLimiterStore.consume`. Two concerns drive the shape of this
module:

1. **Latency.** Consume must be O(1) and avoid syscalls on the steady-
   state path. We allocate one bucket per identifier on first use, then
   only update an in-memory dict.
2. **Concurrency.** FastAPI runs request handlers on the same event
   loop, so two requests for the same key can interleave between the
   read-refill-decrement steps if we don't lock. Each bucket gets its
   own :class:`asyncio.Lock`; the store has a single lock around the
   identifier→bucket dict (to make creation race-safe), but the hot
   path acquires only the per-key lock so concurrent traffic on
   different keys doesn't serialize.

Idle-bucket pruning
-------------------

Each bucket remembers ``last_used_at``. The store sweeps periodically
(every :data:`PRUNE_INTERVAL_SECONDS`) and drops buckets idle for more
than :data:`IDLE_TIMEOUT_SECONDS`. Bound on memory: under one minute of
sustained traffic from N distinct identifiers, memory is O(N) with a
small constant. After idle expiry the bucket vanishes and the next
request from that identifier gets a fresh full bucket — slightly more
permissive than perfectly tracking history, but a reasonable trade for
not leaking memory in the face of millions of distinct IPs.

Multi-replica
-------------

State is per-process. N replicas in front of a load balancer give each
identifier an effective ceiling of N times the configured limit. The
self-hosting docs note this; operators wanting exact shared limits put
a rate-limiting reverse proxy in front of the receiver, which is the
industry default for SaaS.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional


# Sweep cadence and idle threshold for bucket pruning. 60s sweep with
# a 300s idle threshold keeps the dict trimmed without making
# legitimate "slightly bursty" traffic suffer fresh-bucket resets too
# aggressively.
PRUNE_INTERVAL_SECONDS = 60.0
IDLE_TIMEOUT_SECONDS = 300.0


@dataclass
class TokenBucket:
    """One bucket. ``tokens`` is the current allowance (float so refill
    can be smooth across sub-second intervals).

    The bucket holds up to ``capacity`` tokens and refills at
    ``refill_per_second``. ``consume()`` is the only mutator.
    """

    capacity: float
    refill_per_second: float
    tokens: float
    last_refill_at: float
    last_used_at: float
    _lock: asyncio.Lock

    @classmethod
    def new(cls, *, capacity: float, refill_per_second: float, now: float) -> "TokenBucket":
        return cls(
            capacity=capacity,
            refill_per_second=refill_per_second,
            tokens=capacity,  # start full so the first request is always allowed
            last_refill_at=now,
            last_used_at=now,
            _lock=asyncio.Lock(),
        )

    async def consume(self, now: float) -> tuple[bool, int, float]:
        """Try to consume one token.

        Returns ``(allowed, remaining_int, retry_after_seconds)``. When
        allowed, ``retry_after_seconds`` is 0. When denied, it's the
        time until the bucket would have at least one token again.

        ``remaining_int`` is the floor of the post-decision token count,
        suitable for the ``X-RateLimit-Remaining`` header.
        """
        async with self._lock:
            # Refill based on elapsed wall time. Clamped at capacity so
            # a long-idle bucket doesn't accumulate a giant burst.
            elapsed = max(0.0, now - self.last_refill_at)
            self.tokens = min(
                self.capacity, self.tokens + elapsed * self.refill_per_second,
            )
            self.last_refill_at = now
            self.last_used_at = now

            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True, int(self.tokens), 0.0

            # Deficit is how many tokens short of 1.0 we are. Time to
            # recover that deficit at the configured refill rate is the
            # earliest moment a follow-up request would succeed.
            deficit = 1.0 - self.tokens
            if self.refill_per_second <= 0:
                # Defensive: rate of 0 means the bucket never refills.
                # Surface a long retry-after rather than divide by zero.
                retry_after = float("inf")
            else:
                retry_after = deficit / self.refill_per_second
            return False, 0, retry_after


class RateLimiterStore:
    """Per-identifier token buckets with idle pruning.

    The store hands out (and lazily creates) one :class:`TokenBucket`
    per identifier string. All buckets share the same capacity and
    refill rate configured at construction. Per-key limits would
    require schema changes; see the v1.1 backlog for that.
    """

    def __init__(self, *, capacity: int, refill_per_second: float) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity}")
        if refill_per_second <= 0:
            raise ValueError(
                f"refill_per_second must be > 0, got {refill_per_second}",
            )
        self._capacity = float(capacity)
        self._refill_per_second = float(refill_per_second)
        self._buckets: dict[str, TokenBucket] = {}
        # The creation lock protects the dict mutation when two requests
        # for the same brand-new identifier race the first .get().
        self._creation_lock = asyncio.Lock()
        self._last_prune_at: float = 0.0

    @property
    def capacity(self) -> int:
        return int(self._capacity)

    @property
    def refill_per_second(self) -> float:
        return self._refill_per_second

    async def consume(self, identifier: str, *, now: Optional[float] = None) -> tuple[bool, int, float]:
        """Try to consume one token from ``identifier``'s bucket.

        Same return shape as :meth:`TokenBucket.consume`. Allocates a
        full bucket for an identifier we haven't seen before. Triggers
        an idle-bucket prune sweep at most once every
        :data:`PRUNE_INTERVAL_SECONDS`.
        """
        if now is None:
            now = time.monotonic()

        bucket = self._buckets.get(identifier)
        if bucket is None:
            async with self._creation_lock:
                # Re-check inside the lock — another coroutine may have
                # created it between our miss and the lock acquisition.
                bucket = self._buckets.get(identifier)
                if bucket is None:
                    bucket = TokenBucket.new(
                        capacity=self._capacity,
                        refill_per_second=self._refill_per_second,
                        now=now,
                    )
                    self._buckets[identifier] = bucket

        result = await bucket.consume(now)

        # Prune is opportunistic on the hot path. We don't await a
        # background task because the loop event scheduling cost of
        # one extra task per request outweighs the pruning savings.
        if now - self._last_prune_at >= PRUNE_INTERVAL_SECONDS:
            self._prune_idle_buckets(now)

        return result

    def _prune_idle_buckets(self, now: float) -> None:
        """Drop buckets idle longer than :data:`IDLE_TIMEOUT_SECONDS`.

        Synchronous and best-effort: we hold no per-bucket locks here,
        so a bucket in flight during the sweep may briefly survive into
        the next interval. That's fine — the next sweep will catch it,
        and the worst case is one extra dict entry held momentarily.
        """
        self._last_prune_at = now
        stale = [
            key for key, b in self._buckets.items()
            if now - b.last_used_at > IDLE_TIMEOUT_SECONDS
        ]
        for key in stale:
            self._buckets.pop(key, None)

    # ---- Introspection (mainly for tests and /ready integration) ----

    @property
    def num_buckets(self) -> int:
        return len(self._buckets)


__all__ = [
    "IDLE_TIMEOUT_SECONDS",
    "PRUNE_INTERVAL_SECONDS",
    "RateLimiterStore",
    "TokenBucket",
]

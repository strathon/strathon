"""Tests for the in-memory token-bucket rate limiter.

Split into three sections:

* Token-bucket unit tests — pure math: consume, refill, capacity ceiling.
* Store unit tests — per-identifier isolation, idle pruning.
* Middleware integration tests — drive the real FastAPI app via
  TestClient with a tiny limiter swapped in on app.state, then verify
  the public contract (429 status, headers, exempt paths, counter
  emission, disabled-mode passthrough).
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)


DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"
DEFAULT_DB_URL = "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon"


# ---- Token bucket: arithmetic --------------------------------------------


@pytest.mark.asyncio
async def test_token_bucket_starts_full():
    """A new bucket should let the first ``capacity`` requests through
    without any refill happening."""
    from rate_limit import TokenBucket

    b = TokenBucket.new(capacity=5, refill_per_second=10.0, now=0.0)
    for _ in range(5):
        allowed, _, _ = await b.consume(now=0.0)
        assert allowed is True


@pytest.mark.asyncio
async def test_token_bucket_rejects_when_empty():
    """Once the burst is spent, the next request is denied with a
    retry-after that corresponds to one token's worth of refill."""
    from rate_limit import TokenBucket

    b = TokenBucket.new(capacity=2, refill_per_second=1.0, now=0.0)
    # Drain.
    await b.consume(now=0.0)
    await b.consume(now=0.0)

    allowed, remaining, retry_after = await b.consume(now=0.0)
    assert allowed is False
    assert remaining == 0
    # Need 1 token at 1 token/sec = 1.0s
    assert retry_after == pytest.approx(1.0, abs=0.01)


@pytest.mark.asyncio
async def test_token_bucket_refills_over_time():
    """After elapsed time, the bucket gains tokens at refill_per_second."""
    from rate_limit import TokenBucket

    b = TokenBucket.new(capacity=2, refill_per_second=2.0, now=0.0)
    await b.consume(now=0.0)
    await b.consume(now=0.0)
    # 0.5s later: 0.5 * 2 = 1 token refilled
    allowed, _, _ = await b.consume(now=0.5)
    assert allowed is True


@pytest.mark.asyncio
async def test_token_bucket_clamps_at_capacity():
    """Long-idle buckets don't accumulate tokens past capacity."""
    from rate_limit import TokenBucket

    b = TokenBucket.new(capacity=3, refill_per_second=10.0, now=0.0)
    # Drain to 0.
    await b.consume(now=0.0)
    await b.consume(now=0.0)
    await b.consume(now=0.0)
    # 10 seconds later: would-be 100 tokens, clamped to 3.
    allowed_count = 0
    for _ in range(10):
        allowed, _, _ = await b.consume(now=10.0)
        if allowed:
            allowed_count += 1
    # Capacity was 3, plus the refill during the consume loop is
    # negligible because all consumes happen at the same `now`.
    assert allowed_count == 3


@pytest.mark.asyncio
async def test_token_bucket_remaining_decrements_to_zero():
    """The remaining_int returned by consume should reflect the post-
    decision token count."""
    from rate_limit import TokenBucket

    b = TokenBucket.new(capacity=3, refill_per_second=0.1, now=0.0)
    a, rem, _ = await b.consume(now=0.0)
    assert (a, rem) == (True, 2)
    a, rem, _ = await b.consume(now=0.0)
    assert (a, rem) == (True, 1)
    a, rem, _ = await b.consume(now=0.0)
    assert (a, rem) == (True, 0)
    a, rem, _ = await b.consume(now=0.0)
    assert (a, rem) == (False, 0)


# ---- Store: isolation + pruning ------------------------------------------


@pytest.mark.asyncio
async def test_store_creates_independent_buckets_per_identifier():
    """Different identifiers share no state."""
    from rate_limit import RateLimiterStore

    s = RateLimiterStore(capacity=1, refill_per_second=0.1)
    a, _, _ = await s.consume("alice", now=0.0)
    assert a is True
    # Alice has 0 tokens now; further consumes for Alice fail.
    a, _, _ = await s.consume("alice", now=0.0)
    assert a is False
    # Bob's bucket is fresh.
    a, _, _ = await s.consume("bob", now=0.0)
    assert a is True


@pytest.mark.asyncio
async def test_store_rejects_invalid_construction():
    from rate_limit import RateLimiterStore
    with pytest.raises(ValueError):
        RateLimiterStore(capacity=0, refill_per_second=1.0)
    with pytest.raises(ValueError):
        RateLimiterStore(capacity=10, refill_per_second=0.0)


@pytest.mark.asyncio
async def test_store_prunes_idle_buckets():
    """Buckets idle longer than IDLE_TIMEOUT_SECONDS are dropped on the
    next sweep."""
    from rate_limit import (
        IDLE_TIMEOUT_SECONDS, PRUNE_INTERVAL_SECONDS, RateLimiterStore,
    )

    s = RateLimiterStore(capacity=1, refill_per_second=1.0)
    await s.consume("temp-user", now=0.0)
    assert s.num_buckets == 1

    # Far enough in the future to trigger both the prune-interval gate
    # and the idle-bucket threshold.
    future = max(PRUNE_INTERVAL_SECONDS, IDLE_TIMEOUT_SECONDS) + 10.0
    await s.consume("fresh-user", now=future)

    # temp-user pruned, fresh-user added.
    assert s.num_buckets == 1


@pytest.mark.asyncio
async def test_store_concurrent_access_for_same_key_is_serialized():
    """Two concurrent requests for the same identifier must not double-
    spend the bucket (race that the per-bucket lock prevents)."""
    from rate_limit import RateLimiterStore

    s = RateLimiterStore(capacity=1, refill_per_second=0.001)

    # Launch many parallel consumes for the same key. Exactly one
    # should succeed (capacity=1, negligible refill within the test
    # window).
    results = await asyncio.gather(*[
        s.consume("k", now=0.0) for _ in range(20)
    ])
    allowed = sum(1 for r in results if r[0])
    assert allowed == 1


# ---- Middleware integration ----------------------------------------------


@pytest.fixture(scope="module")
def client():
    db_url = os.getenv("DATABASE_URL", DEFAULT_DB_URL)
    os.environ["DATABASE_URL"] = db_url
    try:
        import psycopg
        conn = psycopg.connect(db_url, autocommit=True)
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable")

    from config import get_settings
    from database import get_engine, get_session_maker
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_maker.cache_clear()

    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        yield c


def _auth(key: str = DEV_KEY) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _install_tiny_limiter(client, capacity=2, rps=0.1):
    """Replace the running limiter with a tiny one. Returns the previous
    store so the test can restore it. Tests use small numbers so they
    can exhaust the bucket in a few requests and not have to wait for
    realistic refill rates."""
    import main
    from rate_limit import RateLimiterStore
    previous = main.app.state.rate_limiter
    main.app.state.rate_limiter = RateLimiterStore(
        capacity=capacity, refill_per_second=rps,
    )
    return previous


def test_health_endpoint_is_exempt(client):
    """Even after exhausting the bucket, /health must keep answering."""
    previous = _install_tiny_limiter(client, capacity=1, rps=0.01)
    try:
        # Drain.
        client.get("/v1/policies", headers=_auth())
        client.get("/v1/policies", headers=_auth())  # already over
        # /health must still work and must NOT carry rate-limit headers
        # (exempt paths skip the middleware before the limiter sees them).
        r = client.get("/health")
        assert r.status_code == 200
        assert "X-RateLimit-Limit" not in r.headers
    finally:
        import main
        main.app.state.rate_limiter = previous


def test_ready_endpoint_is_exempt(client):
    previous = _install_tiny_limiter(client, capacity=1, rps=0.01)
    try:
        client.get("/v1/policies", headers=_auth())
        client.get("/v1/policies", headers=_auth())
        r = client.get("/ready")
        # /ready returns 200 or 503 depending on dependency health, never 429.
        assert r.status_code in (200, 503)
        assert "X-RateLimit-Limit" not in r.headers
    finally:
        import main
        main.app.state.rate_limiter = previous


def test_metrics_endpoint_is_exempt(client):
    previous = _install_tiny_limiter(client, capacity=1, rps=0.01)
    try:
        client.get("/v1/policies", headers=_auth())
        client.get("/v1/policies", headers=_auth())
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "X-RateLimit-Limit" not in r.headers
    finally:
        import main
        main.app.state.rate_limiter = previous


def test_success_response_includes_rate_limit_headers(client):
    previous = _install_tiny_limiter(client, capacity=5, rps=0.1)
    try:
        r = client.get("/v1/policies", headers=_auth())
        assert r.status_code == 200
        assert r.headers["X-RateLimit-Limit"] == "5"
        # First call consumed one token; 4 should remain.
        assert r.headers["X-RateLimit-Remaining"] == "4"
    finally:
        import main
        main.app.state.rate_limiter = previous


def test_burst_then_429_with_retry_after(client):
    """After capacity is exhausted, the next request gets 429 with a
    Retry-After header pointing to a positive integer."""
    previous = _install_tiny_limiter(client, capacity=2, rps=0.1)
    try:
        # Burst through capacity.
        assert client.get("/v1/policies", headers=_auth()).status_code == 200
        assert client.get("/v1/policies", headers=_auth()).status_code == 200
        # Next one is throttled.
        r = client.get("/v1/policies", headers=_auth())
        assert r.status_code == 429
        assert r.headers["X-RateLimit-Limit"] == "2"
        assert r.headers["X-RateLimit-Remaining"] == "0"
        # Retry-After is at least 1 second (rps=0.1 → 10s for one token,
        # rounded up).
        retry_after = int(r.headers["Retry-After"])
        assert retry_after >= 1
        body = r.json()
        assert "rate limit exceeded" in body["detail"]
    finally:
        import main
        main.app.state.rate_limiter = previous


def test_429_increments_rejection_counter(client):
    previous = _install_tiny_limiter(client, capacity=1, rps=0.01)
    try:
        import main
        registry = main.app.state.metrics.registry
        before = registry.get_sample_value(
            "strathon_receiver_rate_limit_rejections_total",
            labels={"key_type": "api_key"},
        ) or 0.0

        client.get("/v1/policies", headers=_auth())  # ok
        r = client.get("/v1/policies", headers=_auth())  # throttled
        assert r.status_code == 429

        after = registry.get_sample_value(
            "strathon_receiver_rate_limit_rejections_total",
            labels={"key_type": "api_key"},
        ) or 0.0
        assert after - before == 1.0
    finally:
        import main
        main.app.state.rate_limiter = previous


def test_different_api_keys_have_independent_buckets(client):
    """Two distinct Authorization headers do not share a bucket."""
    previous = _install_tiny_limiter(client, capacity=1, rps=0.01)
    try:
        # Exhaust the dev key's bucket.
        r1 = client.get("/v1/policies", headers=_auth(DEV_KEY))
        assert r1.status_code == 200
        r2 = client.get("/v1/policies", headers=_auth(DEV_KEY))
        assert r2.status_code == 429

        # A different bearer token gets its own bucket. The auth check
        # will reject this token (it's not a real key), but the rate
        # limiter sits in front of auth, so the 401 here proves the
        # request reached the auth layer rather than being throttled
        # by the dev key's exhausted bucket.
        other_headers = {"Authorization": "Bearer stra_different_token_123"}
        r3 = client.get("/v1/policies", headers=other_headers)
        assert r3.status_code in (401, 403), (
            f"expected auth rejection, got {r3.status_code}: {r3.text}"
        )
    finally:
        import main
        main.app.state.rate_limiter = previous


def test_ip_keyed_request_uses_ip_bucket(client):
    """A request with no Authorization header is bucketed by IP."""
    previous = _install_tiny_limiter(client, capacity=1, rps=0.01)
    try:
        import main
        registry = main.app.state.metrics.registry
        before_ip = registry.get_sample_value(
            "strathon_receiver_rate_limit_rejections_total",
            labels={"key_type": "ip"},
        ) or 0.0

        # No auth header. Both attempts share the IP bucket; the second
        # is throttled.
        r1 = client.get("/v1/policies")
        # First either passes the limiter (401 from missing auth) or
        # might fail rate limiter — depends on order. Either way it
        # uses the IP bucket; we then drain explicitly.
        # We don't assert r1.status_code since auth may reject before
        # the second call lands.
        _ = r1
        # Now drain and verify the throttle.
        r2 = client.get("/v1/policies")
        if r2.status_code != 429:
            r2 = client.get("/v1/policies")
        assert r2.status_code == 429, r2.text

        after_ip = registry.get_sample_value(
            "strathon_receiver_rate_limit_rejections_total",
            labels={"key_type": "ip"},
        ) or 0.0
        assert after_ip > before_ip, (
            f"ip-keyed rejection counter didn't increment: {before_ip} -> {after_ip}"
        )
    finally:
        import main
        main.app.state.rate_limiter = previous


def test_x_forwarded_for_overrides_direct_ip(client):
    """When X-Forwarded-For is present, its leftmost value is used as
    the IP. Two different XFFs should produce independent buckets."""
    previous = _install_tiny_limiter(client, capacity=1, rps=0.01)
    try:
        # Drain bucket for the first claimed IP.
        client.get("/v1/policies", headers={"X-Forwarded-For": "10.0.0.1"})
        # Second request from same XFF should be throttled regardless
        # of auth state.
        r2 = client.get("/v1/policies", headers={"X-Forwarded-For": "10.0.0.1"})
        assert r2.status_code == 429, r2.text

        # A different XFF gets a fresh bucket.
        r3 = client.get("/v1/policies", headers={"X-Forwarded-For": "10.0.0.2"})
        assert r3.status_code != 429, (
            f"different XFF should have its own bucket; got {r3.status_code}"
        )
    finally:
        import main
        main.app.state.rate_limiter = previous


def test_disabled_rate_limiter_passes_through(client):
    """Setting app.state.rate_limiter = None should make every request
    behave as if there's no limiter at all — no headers added, no
    429s."""
    import main
    previous = main.app.state.rate_limiter
    main.app.state.rate_limiter = None
    try:
        # Hit the same endpoint many times; would normally throttle,
        # but with the limiter disabled all succeed.
        for _ in range(20):
            r = client.get("/v1/policies", headers=_auth())
            assert r.status_code == 200
            assert "X-RateLimit-Limit" not in r.headers
    finally:
        main.app.state.rate_limiter = previous

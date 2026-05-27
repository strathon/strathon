"""ASGI middleware that applies the in-memory token-bucket rate limiter
to incoming requests.

Wiring
======

The middleware is registered at module load time on ``app``. At request
time it reads ``request.app.state.rate_limiter`` and
``request.app.state.metrics`` — both populated by the lifespan handler.
If the rate limiter is ``None`` (operators disabled it via
``STRATHON_RATE_LIMIT_ENABLED=false``) the middleware is a pass-through.
This deferred lookup keeps the middleware constructor side-effect-free
and lets tests stub the store at runtime.

Identifier resolution
=====================

The bucket key is chosen per request:

* If the request carries an ``Authorization`` header, the key is a
  SHA-256 digest of the header bytes. Two requests presenting the same
  bearer token share a bucket; an invalid bearer also gets its own
  bucket and is throttled before the auth check ever runs (this
  prevents credential-stuffing brute force on
  ``Authorization: Bearer <guess>``).
* Otherwise the key is the client IP. We accept ``X-Forwarded-For``
  when present — the leftmost address is conventionally the original
  client. Operators behind a reverse proxy should ensure that proxy
  strips client-supplied XFF and writes its own, which is standard
  reverse-proxy hygiene.

Exempt paths
============

Health/readiness/metrics endpoints are never throttled. These are
probes that must keep responding even (especially) when the receiver
is under load.

Response headers
================

Every rate-limited response (success or 429) carries
``X-RateLimit-Limit`` and ``X-RateLimit-Remaining``. On 429 we also
emit ``Retry-After`` (seconds, integer) per RFC 9110, and a stable
JSON body so clients can branch on ``detail``.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from typing import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


logger = logging.getLogger(__name__)


# Paths that are NEVER rate-limited. Probes from Kubernetes / load
# balancers / Prometheus must always succeed; if they're throttled the
# operator loses observability exactly when they need it most.
EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/ready", "/metrics"})


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket rate limit driven by ``request.app.state.rate_limiter``."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        store = getattr(request.app.state, "rate_limiter", None)
        if store is None:
            # Operator disabled it via STRATHON_RATE_LIMIT_ENABLED=false,
            # or we're in a test that didn't initialize the store.
            return await call_next(request)

        identifier, key_type = _identifier_for(request)
        now = time.monotonic()
        allowed, remaining, retry_after = await store.consume(identifier, now=now)

        limit_str = str(store.capacity)

        if not allowed:
            metrics = getattr(request.app.state, "metrics", None)
            if metrics is not None and hasattr(metrics, "rate_limit_rejections"):
                metrics.rate_limit_rejections.labels(key_type=key_type).inc()

            # Round UP retry-after so a 0.4s wait surfaces as 1s — a
            # client that re-attempts at the floor would immediately
            # fail again.
            retry_seconds = max(1, math.ceil(retry_after))
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"rate limit exceeded, retry in {retry_seconds}s",
                },
                headers={
                    "Retry-After": str(retry_seconds),
                    "X-RateLimit-Limit": limit_str,
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        # Stamp headers on the downstream response so well-behaved
        # clients can self-throttle before they hit 429.
        response.headers["X-RateLimit-Limit"] = limit_str
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response


def _identifier_for(request: Request) -> tuple[str, str]:
    """Return ``(identifier, key_type)`` for the rate-limit bucket.

    ``key_type`` is one of ``"api_key"`` or ``"ip"`` and is recorded on
    the rejections counter so operators can tell whether throttling is
    hitting authenticated traffic (typically a runaway agent) or
    unauthenticated traffic (typically credential stuffing).
    """
    auth = request.headers.get("authorization")
    if auth:
        # Hash so we don't keep the secret in a dict key. SHA-256 of
        # the raw header bytes — collision-resistant and the same key
        # always yields the same hash, which is all we need.
        digest = hashlib.sha256(auth.encode("utf-8", errors="replace")).hexdigest()
        return f"key:{digest}", "api_key"

    # X-Forwarded-For: leftmost is conventionally the original client.
    # We trust whatever the proxy in front of us claims; operators
    # without a proxy in front will see direct-connection IPs anyway.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        ip = xff.split(",", 1)[0].strip()
        if ip:
            return f"ip:{ip}", "ip"

    client = request.client
    if client is not None:
        return f"ip:{client.host}", "ip"

    # Fallback for clients with no resolvable address (rare; happens
    # with some ASGI test clients). Bucket all such requests together —
    # they shouldn't happen in production.
    return "ip:unknown", "ip"


__all__ = [
    "EXEMPT_PATHS",
    "RateLimitMiddleware",
]

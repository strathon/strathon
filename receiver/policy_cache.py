"""Per-project policy cache for the ingest hot path.

Every /v1/traces ingest call needs the project's enabled policies to evaluate
spans. Querying them from the database on every request adds a DB round-trip
to the hot path. This cache holds the enabled-policy list per project for a
short TTL so the steady-state ingest path hits memory, not the database.

Staleness model (deliberate, matches the SDK's pull-and-refresh model):
  * TTL-bounded. After a policy change, ingest workers pick up the new policy
    set within at most POLICY_CACHE_TTL_SECONDS. The SDK already accepts ~30s
    policy staleness; the receiver default here is stricter (5s).
  * Per-process. With multiple uvicorn workers each worker has its own cache;
    each refreshes independently within the TTL. There is no cross-worker
    invalidation by design — bounded TTL staleness is simpler and has no extra
    failure modes. Instant cross-worker invalidation (LISTEN/NOTIFY) is a
    future enhancement, not needed for correctness given the bounded TTL.
  * Explicit invalidation. Policy create/update/delete in this process call
    invalidate_project() so the editing worker is immediately consistent.

Fail-safe: a cache miss or refresh error falls through to a direct DB query
at the call site; the cache never swallows policy loading.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from uuid import UUID

POLICY_CACHE_TTL_SECONDS = float(
    os.environ.get("STRATHON_POLICY_CACHE_TTL", "5.0")
)

# project_id -> (expires_at_monotonic, policies)
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_lock = asyncio.Lock()


def invalidate_project(project_id: UUID | str) -> None:
    """Drop the cached policies for a project (call on policy create/update/delete)."""
    _cache.pop(str(project_id), None)


def invalidate_all() -> None:
    """Drop the entire cache (e.g. on a config reload)."""
    _cache.clear()


async def get_policies(
    project_id: UUID | str,
    loader,
) -> list[dict[str, Any]]:
    """Return enabled policies for a project, from cache or via loader().

    loader is an async callable taking no args that returns the freshly-loaded
    policy dict list (the caller closes over its session/project). It is only
    invoked on a cache miss or expiry.
    """
    key = str(project_id)
    now = time.monotonic()

    cached = _cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]

    async with _lock:
        # Re-check after acquiring the lock: another coroutine may have just
        # refreshed it while we waited.
        cached = _cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        policies = await loader()
        _cache[key] = (now + POLICY_CACHE_TTL_SECONDS, policies)
        return policies

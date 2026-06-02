"""Tests for the ingest policy cache (policy_cache.py)."""

from __future__ import annotations

import asyncio
import uuid

import policy_cache


def setup_function():
    policy_cache.invalidate_all()


def test_loader_called_once_within_ttl():
    pid = uuid.uuid4()
    calls = {"n": 0}

    async def loader():
        calls["n"] += 1
        return [{"id": "p1"}]

    async def run():
        a = await policy_cache.get_policies(pid, loader)
        b = await policy_cache.get_policies(pid, loader)
        return a, b

    a, b = asyncio.run(run())
    assert a == b == [{"id": "p1"}]
    assert calls["n"] == 1  # second call served from cache


def test_ttl_expiry_triggers_reload(monkeypatch):
    pid = uuid.uuid4()
    calls = {"n": 0}

    async def loader():
        calls["n"] += 1
        return [{"id": f"load-{calls['n']}"}]

    # Force a zero TTL so the entry is always expired.
    monkeypatch.setattr(policy_cache, "POLICY_CACHE_TTL_SECONDS", 0.0)

    async def run():
        await policy_cache.get_policies(pid, loader)
        await policy_cache.get_policies(pid, loader)

    asyncio.run(run())
    assert calls["n"] == 2  # expired each time -> reloaded


def test_invalidate_forces_reload():
    pid = uuid.uuid4()
    calls = {"n": 0}

    async def loader():
        calls["n"] += 1
        return [{"id": "x"}]

    async def run():
        await policy_cache.get_policies(pid, loader)
        policy_cache.invalidate_project(pid)
        await policy_cache.get_policies(pid, loader)

    asyncio.run(run())
    assert calls["n"] == 2


def test_per_project_isolation():
    pid_a, pid_b = uuid.uuid4(), uuid.uuid4()

    async def loader_a():
        return [{"id": "a"}]

    async def loader_b():
        return [{"id": "b"}]

    async def run():
        a = await policy_cache.get_policies(pid_a, loader_a)
        b = await policy_cache.get_policies(pid_b, loader_b)
        return a, b

    a, b = asyncio.run(run())
    assert a == [{"id": "a"}]
    assert b == [{"id": "b"}]  # one project's cache never serves another's

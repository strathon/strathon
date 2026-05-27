"""Tests for strathon.policy.halt_enforcer.

Coverage:
  * check_halt with no cached halts returns ALLOW_HALT
  * project-scope halt matches every call
  * agent-scope halt matches only when agent_id matches scope_value
  * agent-scope halt with non-matching agent_id allows
  * scope_value is read from strathon.agent.id OR gen_ai.agent.id
  * multiple halts: most recently set (first in list) wins
  * refresh() success populates cache
  * refresh() failure preserves last cache (fail-open property)
  * set_halts_for_testing bypasses network
  * stop() joins the background thread cleanly
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch


from strathon.policy.halt_enforcer import HaltEnforcer


# ---- Construction and check_halt with no data --------------------------


def _make_enforcer(**kwargs):
    return HaltEnforcer(
        endpoint="http://localhost:0",
        api_key="test",
        project_id="proj-1",
        refresh_interval_sec=0.05,  # fast for tests
        **kwargs,
    )


def test_empty_cache_allows_all_calls():
    e = _make_enforcer()
    d = e.check_halt({"name": "tool.x", "attrs": {}})
    assert d.is_allow
    assert not d.is_halt


def test_set_halts_for_testing_bypasses_network():
    e = _make_enforcer()
    e.set_halts_for_testing([
        {"id": 1, "scope": "project", "scope_value": None,
         "state": "halted", "reason": "test"},
    ])
    assert len(e.halts) == 1


# ---- Scope matching ---------------------------------------------------


def test_project_scope_halt_matches_every_call():
    e = _make_enforcer()
    e.set_halts_for_testing([
        {"id": 1, "scope": "project", "scope_value": None,
         "state": "halted", "reason": "killswitch"},
    ])
    # No agent_id in attrs — project halts don't care about agent
    d = e.check_halt({"name": "tool.x", "attrs": {}})
    assert d.is_halt
    assert d.halt_id == 1
    assert d.scope == "project"
    assert d.scope_value is None
    assert d.reason == "killswitch"


def test_agent_scope_halt_matches_when_agent_id_equals_scope_value():
    e = _make_enforcer()
    e.set_halts_for_testing([
        {"id": 5, "scope": "agent", "scope_value": "agent-7",
         "state": "halted", "reason": "agent-7 stop"},
    ])
    d = e.check_halt({"name": "tool.x", "attrs": {"strathon.agent.id": "agent-7"}})
    assert d.is_halt
    assert d.halt_id == 5
    assert d.scope == "agent"
    assert d.scope_value == "agent-7"


def test_agent_scope_halt_does_not_match_other_agents():
    e = _make_enforcer()
    e.set_halts_for_testing([
        {"id": 5, "scope": "agent", "scope_value": "agent-7",
         "state": "halted", "reason": "agent-7 stop"},
    ])
    # Same call but from a different agent
    d = e.check_halt({"name": "tool.x", "attrs": {"strathon.agent.id": "agent-99"}})
    assert d.is_allow


def test_agent_scope_halt_uses_gen_ai_agent_id_fallback():
    """gen_ai.agent.id is the OTel standard attribute; strathon.agent.id
    is the SDK's own. Either should be checked."""
    e = _make_enforcer()
    e.set_halts_for_testing([
        {"id": 5, "scope": "agent", "scope_value": "agent-7",
         "state": "halted", "reason": "stop"},
    ])
    d = e.check_halt({"name": "tool.x", "attrs": {"gen_ai.agent.id": "agent-7"}})
    assert d.is_halt
    assert d.halt_id == 5


def test_strathon_agent_id_takes_priority_over_gen_ai():
    """Both attrs set: SDK-native attribute wins (it's more specific)."""
    e = _make_enforcer()
    e.set_halts_for_testing([
        {"id": 5, "scope": "agent", "scope_value": "agent-A",
         "state": "halted", "reason": "stop"},
    ])
    d = e.check_halt({"name": "tool.x", "attrs": {
        "strathon.agent.id": "agent-A",
        "gen_ai.agent.id": "agent-B",
    }})
    assert d.is_halt


def test_no_agent_id_no_agent_scope_match():
    e = _make_enforcer()
    e.set_halts_for_testing([
        {"id": 5, "scope": "agent", "scope_value": "agent-7",
         "state": "halted", "reason": "stop"},
    ])
    d = e.check_halt({"name": "tool.x", "attrs": {}})
    assert d.is_allow


# ---- Multiple halts ---------------------------------------------------


def test_first_match_in_list_wins():
    """The receiver returns halts ordered by set_at DESC. The SDK
    treats the first match as authoritative — the most recent halt
    is what the caller sees."""
    e = _make_enforcer()
    e.set_halts_for_testing([
        # Newest first
        {"id": 10, "scope": "agent", "scope_value": "agent-7",
         "state": "halted", "reason": "newest"},
        {"id": 9, "scope": "agent", "scope_value": "agent-7",
         "state": "halted", "reason": "older"},
    ])
    d = e.check_halt({"name": "tool.x", "attrs": {"strathon.agent.id": "agent-7"}})
    assert d.halt_id == 10
    assert d.reason == "newest"


def test_project_halt_supersedes_agent_halts_when_listed_first():
    """If both a project and agent halt are active, the receiver lists
    them in set_at order. Whichever was set more recently fires first."""
    e = _make_enforcer()
    e.set_halts_for_testing([
        # Project halt is the most recent — wins for every call
        {"id": 20, "scope": "project", "scope_value": None,
         "state": "halted", "reason": "project-wide"},
        {"id": 10, "scope": "agent", "scope_value": "agent-7",
         "state": "halted", "reason": "agent-specific"},
    ])
    d = e.check_halt({"name": "tool.x", "attrs": {"strathon.agent.id": "agent-7"}})
    assert d.halt_id == 20
    assert d.scope == "project"


# ---- refresh() with mocked HTTP ---------------------------------------


def test_refresh_success_populates_cache():
    """Mock urlopen to return a successful sync response and verify
    the cache is populated."""
    e = _make_enforcer()
    fake_payload = {
        "halts": [
            {"id": 1, "scope": "agent", "scope_value": "a",
             "state": "halted", "reason": "r"},
        ],
        "budgets": [],
        "synced_at_unix_nano": 1_700_000_000_000_000_000,
    }
    with patch("strathon.policy.halt_enforcer.urlopen") as mock_urlopen:
        mock_resp = _mock_response(fake_payload)
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        assert e.refresh() is True

    halts = e.halts
    assert len(halts) == 1
    assert halts[0]["id"] == 1
    assert e.last_refresh_error is None


def test_refresh_network_failure_preserves_cache():
    """Fail-open property: a network error must NOT clear the cache."""
    from urllib.error import URLError

    e = _make_enforcer()
    # Prime the cache with a halt
    e.set_halts_for_testing([
        {"id": 1, "scope": "project", "scope_value": None,
         "state": "halted", "reason": "preserved"},
    ])

    # Now simulate a network outage
    with patch("strathon.policy.halt_enforcer.urlopen", side_effect=URLError("boom")):
        assert e.refresh() is False

    # The cache is still in force
    halts = e.halts
    assert len(halts) == 1
    assert halts[0]["reason"] == "preserved"
    assert "network error" in (e.last_refresh_error or "")


def test_refresh_handles_non_list_halts_field():
    """Defensive: if the server returns something weird in the halts
    field, we log and treat it as empty rather than crashing."""
    e = _make_enforcer()
    weird_payload = {"halts": "not-a-list", "budgets": []}
    with patch("strathon.policy.halt_enforcer.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = _mock_response(weird_payload)
        assert e.refresh() is True
    assert e.halts == []


def test_refresh_handles_missing_halts_field():
    e = _make_enforcer()
    with patch("strathon.policy.halt_enforcer.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = _mock_response({})
        assert e.refresh() is True
    assert e.halts == []


# ---- Lifecycle: start/stop --------------------------------------------


def test_start_does_initial_sync_refresh():
    """start() should do one synchronous fetch before spawning the
    background thread, so the first check_halt has data."""
    e = _make_enforcer()
    fake_payload = {
        "halts": [
            {"id": 1, "scope": "project", "scope_value": None,
             "state": "halted", "reason": "r"},
        ],
        "budgets": [],
    }
    with patch("strathon.policy.halt_enforcer.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = _mock_response(fake_payload)
        e.start()
        try:
            # Cache populated immediately after start()
            assert len(e.halts) == 1
        finally:
            e.stop()


def test_stop_joins_thread_cleanly():
    """stop() must terminate the background thread within the join
    timeout, even if the thread is sleeping in its wait."""
    e = _make_enforcer()
    with patch("strathon.policy.halt_enforcer.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = _mock_response({"halts": []})
        e.start()
        # Give the loop a chance to enter its wait state
        time.sleep(0.02)
        e.stop()
        # Thread is gone
        assert e._thread is None


def test_background_thread_refreshes_periodically():
    """With a short interval the cache should update on its own."""
    e = _make_enforcer()

    call_count = {"n": 0}
    payloads = [
        {"halts": [], "budgets": []},
        {"halts": [{"id": 99, "scope": "project", "scope_value": None,
                    "state": "halted", "reason": "fresh"}], "budgets": []},
        {"halts": [{"id": 99, "scope": "project", "scope_value": None,
                    "state": "halted", "reason": "fresh"}], "budgets": []},
    ]

    def fake_urlopen(*args, **kwargs):
        i = min(call_count["n"], len(payloads) - 1)
        call_count["n"] += 1
        cm = patch.object  # noqa: F841 (unused; placeholder)
        return _ContextManager(_mock_response(payloads[i]))

    with patch("strathon.policy.halt_enforcer.urlopen", side_effect=fake_urlopen):
        e.start()
        try:
            # First refresh in start() yields empty cache
            assert e.halts == []
            # Wait long enough for at least one background tick
            deadline = time.time() + 1.0
            while time.time() < deadline:
                if e.halts:
                    break
                time.sleep(0.05)
            assert len(e.halts) == 1
            assert e.halts[0]["id"] == 99
        finally:
            e.stop()


# ---- helpers ----------------------------------------------------------


class _ContextManager:
    """Mimic the urlopen context manager shape for tests."""

    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, exc_type, exc, tb):
        return False


def _mock_response(payload: Any):
    """Build an object with a .read() that returns JSON-encoded bytes."""
    import json

    class _Resp:
        def read(self):
            return json.dumps(payload).encode("utf-8")

    return _Resp()

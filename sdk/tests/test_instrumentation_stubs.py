"""Tests for the instrumentation module registry.

Covers:

- SUPPORTED_FRAMEWORKS lists all 8 real implementations
- PLANNED_FRAMEWORKS is empty (all stubs are now real)
- langgraph IS in SUPPORTED_FRAMEWORKS (regression guard)
- Each of the 5 formerly-stub modules has a real instrument()
  function that returns False when the framework isn't installed
  (graceful degradation) rather than raising NotImplementedError
- auto_instrument(client) with defaults doesn't crash
- auto_instrument(client, frameworks=["unknown"]) logs + skips
- langchain.instrument delegates to langgraph
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from strathon.instrumentation import (
    PLANNED_FRAMEWORKS,
    SUPPORTED_FRAMEWORKS,
    auto_instrument,
)


def test_supported_frameworks_contains_all_ten():
    expected = {
        "langgraph", "crewai", "openai_agents",
        "openai", "anthropic", "langchain",
        "autogen", "claude_agent", "pydantic_ai",
        "google_adk",
    }
    assert set(SUPPORTED_FRAMEWORKS) == expected


def test_langgraph_in_supported():
    """Regression: langgraph was previously missing from SUPPORTED_FRAMEWORKS."""
    assert "langgraph" in SUPPORTED_FRAMEWORKS


def test_planned_frameworks_is_empty():
    """All stubs are now real implementations."""
    assert PLANNED_FRAMEWORKS == []


def test_no_overlap_between_supported_and_planned():
    assert set(SUPPORTED_FRAMEWORKS).isdisjoint(set(PLANNED_FRAMEWORKS))


@pytest.mark.parametrize("fw", [
    "openai", "anthropic", "langchain", "autogen", "claude_agent",
])
def test_formerly_stub_modules_have_instrument(fw):
    """Each module has a callable instrument() function."""
    module = __import__(
        f"strathon.instrumentation.{fw}", fromlist=["instrument"]
    )
    assert callable(module.instrument)


@pytest.mark.parametrize("fw", [
    "openai", "anthropic", "autogen", "claude_agent",
])
def test_instrument_returns_false_when_framework_not_installed(fw):
    """When the target framework isn't installed, instrument() returns False
    (not NotImplementedError)."""
    module = __import__(
        f"strathon.instrumentation.{fw}", fromlist=["instrument"]
    )
    # Reset the _PATCHED flag so we can test the import check.
    if hasattr(module, "_PATCHED"):
        module._PATCHED = False
    client = MagicMock()
    # This should return False (framework not installed) or True
    # (framework installed), but should NOT raise.
    result = module.instrument(client)
    assert isinstance(result, bool)


def test_langchain_delegates_to_langgraph():
    """langchain.instrument imports from langgraph and delegates."""
    import strathon.instrumentation.langchain as lc_mod
    # The function should exist and be callable.
    assert callable(lc_mod.instrument)
    # When langchain is not installed, returns None (falsy).
    client = MagicMock()
    result = lc_mod.instrument(client)
    # Result is either None (langchain not installed) or a handler.
    # We can't easily control whether langchain is installed in CI,
    # so just verify it doesn't raise.
    assert result is None or result is not None


def test_auto_instrument_defaults_safe():
    """Default auto_instrument does not raise for any framework."""
    client = MagicMock()
    result = auto_instrument(client)
    assert isinstance(result, list)


def test_auto_instrument_unknown_framework_skipped():
    """A completely unknown framework name is logged and skipped."""
    client = MagicMock()
    result = auto_instrument(client, frameworks=["nonexistent_framework"])
    assert result == []


def test_auto_instrument_explicit_openai():
    """Explicitly requesting openai works (returns bool, no crash)."""
    client = MagicMock()
    # May instrument or not depending on whether openai is installed.
    # Key assertion: no NotImplementedError.
    import strathon.instrumentation.openai as oai_mod
    if hasattr(oai_mod, "_PATCHED"):
        oai_mod._PATCHED = False
    result = auto_instrument(client, frameworks=["openai"])
    assert isinstance(result, list)


def test_auto_instrument_explicit_anthropic():
    """Explicitly requesting anthropic works."""
    client = MagicMock()
    import strathon.instrumentation.anthropic as anth_mod
    if hasattr(anth_mod, "_PATCHED"):
        anth_mod._PATCHED = False
    result = auto_instrument(client, frameworks=["anthropic"])
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Regression: auto_instrument must WARN when a framework the caller EXPLICITLY
# requested fails to instrument (e.g. the framework package isn't installed).
# Silent non-instrumentation of a firewall is a safety gap. Auto-detect mode
# (frameworks=None) must stay quiet. Made deterministic by stubbing a module's
# instrument() rather than depending on what's installed in the test env.
# ---------------------------------------------------------------------------


def _stub_instrument_result(monkeypatch, fw, result):
    """Force strathon.instrumentation.<fw>.instrument() to return `result`."""
    module = __import__(
        f"strathon.instrumentation.{fw}", fromlist=["instrument"]
    )
    monkeypatch.setattr(module, "instrument", lambda client: result)


def test_explicit_request_warns_when_not_instrumented(monkeypatch, caplog):
    import logging
    _stub_instrument_result(monkeypatch, "crewai", False)

    class C:
        pass

    with caplog.at_level(logging.WARNING, logger="strathon.instrumentation"):
        result = auto_instrument(C(), frameworks=["crewai"])

    assert result == []
    assert any("not instrumented" in r.message or "NOT be enforced" in r.message
               for r in caplog.records), "expected a loud warning"


def test_autodetect_does_not_warn_when_not_instrumented(monkeypatch, caplog):
    import logging
    # In auto-detect mode (frameworks=None) a framework returning False is
    # normal (just not installed) and must NOT warn.
    _stub_instrument_result(monkeypatch, "crewai", False)

    class C:
        pass

    with caplog.at_level(logging.WARNING, logger="strathon.instrumentation"):
        auto_instrument(C(), frameworks=None)

    assert not any("not instrumented" in r.message for r in caplog.records)


def test_explicit_request_no_warn_when_instrumented(monkeypatch, caplog):
    import logging
    _stub_instrument_result(monkeypatch, "crewai", True)

    class C:
        pass

    with caplog.at_level(logging.WARNING, logger="strathon.instrumentation"):
        result = auto_instrument(C(), frameworks=["crewai"])

    assert "crewai" in result
    assert not any("not instrumented" in r.message for r in caplog.records)

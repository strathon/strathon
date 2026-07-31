"""The linear-time regex engine must be observable via /ready.

google-re2 prevents ReDoS on the ingest path. If it is missing the code falls
back to stdlib re, which is vulnerable, and that must not happen silently: the
readiness check surfaces it so a broken deployment fails its probe.
"""

from __future__ import annotations

import os
import sys

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)


def test_regex_engine_ok_matches_re2_availability():
    import regex_engine

    # In a correctly-built environment re2 is present and the check passes.
    assert regex_engine.regex_engine_ok() is regex_engine.RE2_AVAILABLE


def test_ready_check_fails_when_engine_unavailable(monkeypatch):
    """When re2 is reported unavailable, the /ready sub-check is 'failed' so the
    overall probe returns not_ready (503)."""
    import regex_engine
    from api import health

    monkeypatch.setattr(regex_engine, "RE2_AVAILABLE", False)
    monkeypatch.setattr(regex_engine, "regex_engine_ok", lambda: False)

    check = health._check_regex_engine()
    assert check["status"] == "failed"
    assert "re2" in check["reason"].lower() or "regex" in check["reason"].lower()


def test_ready_check_ok_when_engine_available(monkeypatch):
    import regex_engine
    from api import health

    monkeypatch.setattr(regex_engine, "regex_engine_ok", lambda: True)
    assert health._check_regex_engine()["status"] == "ok"

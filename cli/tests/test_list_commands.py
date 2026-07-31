"""Smoke tests for the CLI list commands.

The CLI had no tests, which is why three user-facing breakages shipped at once:
list commands assumed a {"data": [...]} envelope while the receiver returns
resource-named keys for most endpoints (so eight commands printed "none found"
against populated data), `audit list` called a route that does not exist, and
`policies list` crashed on the wrong key. These tests mock api_get to return the
receiver's real envelope shapes and assert each command surfaces the rows -- a
dozen of them would have caught all of it.
"""

from __future__ import annotations

import os
import sys

import pytest
from click.testing import CliRunner

_CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _CLI_DIR)

from strathon_cli import main as cli_main  # noqa: E402
from strathon_cli.main import cli, extract_list  # noqa: E402


# ---- extract_list unit tests ------------------------------------------------


def test_extract_list_prefers_resource_key():
    assert extract_list({"halts": [{"id": 1}]}, "halts") == [{"id": 1}]


def test_extract_list_falls_back_to_data():
    assert extract_list({"data": [{"id": 2}]}, "halts") == [{"id": 2}]


def test_extract_list_handles_bare_list():
    assert extract_list([{"id": 3}], "halts") == [{"id": 3}]


def test_extract_list_returns_empty_when_no_list():
    # The failure that mattered: a dict with the rows under an unexpected key
    # must not be mistaken for empty -- but a genuinely listless response is [].
    assert extract_list({"intervention_default_action": "block"}, "policies") == []


# ---- list-command smoke tests ----------------------------------------------
#
# Each mocks api_get to return the receiver's REAL envelope for that endpoint
# and asserts the row is rendered (not the empty-state message). The envelope
# key per endpoint is the crux of what these tests cover.

_CASES = [
    # (argv, receiver envelope, a substring that only appears if the row rendered)
    (["policies", "list"], {"policies": [{"id": "p1", "name": "block-x", "action": "block", "enabled": True, "priority": 1}], "intervention_default_action": "allow"}, "block-x"),
    (["halts", "list"], {"halts": [{"id": "h1", "scope": "agent", "reason": "manual"}]}, "h1"),
    (["agents", "list"], {"agents": [{"agent_name": "agent-x", "risk_score": "low"}]}, "agent-x"),
    (["budgets", "list"], {"budgets": [{"id": "b1", "name": "monthly-cap"}]}, "monthly-cap"),
    (["keys", "list"], {"api_keys": [{"id": "k1", "name": "ci-key", "key_prefix": "stra_ci"}]}, "ci-key"),
]


@pytest.mark.parametrize("argv,envelope,needle", _CASES, ids=[c[0][0] for c in _CASES])
def test_list_command_surfaces_rows(monkeypatch, argv, envelope, needle):
    monkeypatch.setattr(cli_main, "api_get", lambda *a, **k: envelope)
    result = CliRunner().invoke(cli, argv)
    assert result.exit_code == 0, result.output
    assert needle in result.output, (
        f"{argv} did not render the row; got:\n{result.output}"
    )
    assert "No " not in result.output.split("\n")[0], (
        f"{argv} printed an empty-state message despite data:\n{result.output}"
    )


def test_audit_list_uses_the_events_route(monkeypatch):
    """`audit list` must call /v1/audit/events, not the non-existent
    /v1/audit. Assert the path the command requests."""
    called = {}

    def fake_get(path, params=None):
        called["path"] = path
        return {"events": [{"id": "e1", "action": "policy.read"}]}

    monkeypatch.setattr(cli_main, "api_get", fake_get)
    result = CliRunner().invoke(cli, ["audit", "list"])
    assert result.exit_code == 0, result.output
    assert called["path"] == "/v1/audit/events", called["path"]

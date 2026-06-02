"""Tests for the Strathon egress proxy (egress_proxy.py).

The egress proxy is a mitmproxy addon. These tests exercise the addon's
enforcement logic directly using mitmproxy's own test helpers
(mitmproxy.test.tflow / taddons), so they validate the real request/response
hooks rather than a reimplementation.

If mitmproxy is not installed, the whole module is skipped — the addon only
runs inside a mitmproxy process, so testing it requires mitmproxy present.
"""

from __future__ import annotations

import pytest

mitmproxy = pytest.importorskip("mitmproxy")
from mitmproxy.test import taddons, tflow  # noqa: E402

import egress_proxy  # noqa: E402


def _addon():
    return egress_proxy.StrathonEgressAddon()


def _request_flow(url="http://upstream.test/api", method="POST", body=""):
    f = tflow.tflow()
    f.request.method = method
    f.request.url = url
    if body:
        f.request.set_text(body)
    return f


def test_credential_in_request_body_is_blocked():
    addon = _addon()
    f = _request_flow(body="leaking AKIAIOSFODNN7EXAMPLE in the body here")
    with taddons.context(addon):
        addon.request(f)
    assert f.response is not None
    assert f.response.status_code == 403
    assert f.response.headers.get("X-Strathon-Block-Reason") == "credential-leak"


def test_clean_request_with_no_policies_passes():
    addon = _addon()
    addon._policies = []  # no policies loaded
    f = _request_flow(body="just a normal request payload, nothing secret")
    with taddons.context(addon):
        addon.request(f)
    # No response set means the request was allowed to proceed upstream.
    assert f.response is None


def test_policy_block_via_pulled_policies():
    addon = _addon()
    # Simulate a pulled policy that blocks POSTs (http.post tool name).
    addon._policies = [{
        "id": "p1",
        "name": "block_external_post",
        "enabled": True,
        "action": "block",
        "applies_to": [],
        "match_expression": 'attrs["strathon.tool.name"] == "http.post"',
        "priority": 100,
    }]
    f = _request_flow(method="POST", body="benign body, no credentials")
    with taddons.context(addon):
        addon.request(f)
    assert f.response is not None
    assert f.response.status_code == 403
    assert f.response.headers.get("X-Strathon-Block-Reason") == "policy"


def test_policy_allow_when_expression_does_not_match():
    addon = _addon()
    addon._policies = [{
        "id": "p1", "name": "block_gets_only", "enabled": True, "action": "block",
        "applies_to": [],
        "match_expression": 'attrs["strathon.tool.name"] == "http.get"',
        "priority": 100,
    }]
    f = _request_flow(method="POST", body="benign body")  # POST, rule targets GET
    with taddons.context(addon):
        addon.request(f)
    assert f.response is None  # not blocked


def test_eval_error_fails_closed(monkeypatch):
    addon = _addon()
    addon._policies = [{
        "id": "p1", "name": "x", "enabled": True, "action": "block",
        "applies_to": [], "match_expression": "true", "priority": 1,
    }]

    # evaluate_for_span is resilient (it swallows per-policy crashes), so to
    # exercise the addon's fail-closed branch we force the evaluator itself
    # to raise — simulating the policies module being unavailable/broken.
    def boom(*a, **k):
        raise RuntimeError("policy engine unavailable")

    monkeypatch.setattr("policies.evaluate_for_span", boom)
    verdict = addon._evaluate_policies("POST", "http://upstream.test/x")
    assert verdict["action"] == "block"
    assert verdict["policy_name"] == "_fail_closed"


def test_response_credentials_are_redacted():
    addon = _addon()
    f = _request_flow()
    f.response = tflow.tflow(resp=True).response
    f.response.set_text("response leaking AKIAIOSFODNN7EXAMPLE to the agent")
    with taddons.context(addon):
        addon.response(f)
    body = f.response.get_text()
    assert "AKIAIOSFODNN7EXAMPLE" not in body
    assert f.response.headers.get("X-Strathon-Redacted") is not None

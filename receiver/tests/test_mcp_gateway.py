"""Tests for the MCP Security Gateway (mcp_gateway.py + api/mcp.py).

Two layers:
  1. Unit tests for MCPSecurityGateway enforcement logic, with the upstream
     forward monkeypatched (no network). These prove block / allow / blocklist
     / fail-closed / require_approval / response credential scanning.
  2. API tests for POST /v1/mcp/proxy: scope gating and end-to-end block via a
     real created policy, with the upstream forward patched.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import psycopg
import pytest

DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"


# --------------------------------------------------------------------------
# Layer 1: unit tests for the evaluator (no DB, no network)
# --------------------------------------------------------------------------

def _make_gateway(policies, **kw):
    from mcp_gateway import MCPSecurityGateway

    return MCPSecurityGateway(
        upstream_url="http://upstream.invalid/mcp", policies=policies, **kw
    )


def _tool_call(name, args=None, req_id=1):
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": args or {}},
    }


def test_allow_forwards_when_no_policy_matches(monkeypatch):
    gw = _make_gateway(policies=[])
    forwarded = {}

    async def fake_forward(req):
        forwarded["called"] = True
        return {"jsonrpc": "2.0", "id": req.get("id"), "result": {"ok": True}}

    monkeypatch.setattr(gw, "_forward", fake_forward)
    resp = asyncio.run(gw.handle_request(_tool_call("safe_tool")))
    assert forwarded.get("called") is True
    assert resp["result"]["ok"] is True


def test_block_policy_blocks_tool_call(monkeypatch):
    # A policy that matches the tool by name with action=block.
    policies = [{
        "id": uuid.uuid4(),
        "name": "block_send_email",
        "enabled": True,
        "action": "block",
        "applies_to": [],
        "match_expression": 'attrs["strathon.tool.name"] == "send_email"',
        "priority": 100,
    }]
    gw = _make_gateway(policies=policies)

    async def fake_forward(req):  # must NOT be called
        raise AssertionError("blocked tool call must not forward")

    monkeypatch.setattr(gw, "_forward", fake_forward)
    resp = asyncio.run(gw.handle_request(_tool_call("send_email")))
    assert "error" in resp
    assert resp["error"]["code"] == -32040
    assert "block_send_email" in resp["error"]["message"]


def test_blocklist_blocks_without_forwarding(monkeypatch):
    gw = _make_gateway(policies=[], blocked_tools=["dangerous_tool"])

    async def fake_forward(req):
        raise AssertionError("blocklisted tool must not forward")

    monkeypatch.setattr(gw, "_forward", fake_forward)
    resp = asyncio.run(gw.handle_request(_tool_call("dangerous_tool")))
    assert resp["error"]["code"] == -32040


def test_fail_closed_blocks_on_eval_error(monkeypatch):
    gw = _make_gateway(policies=[{"bad": "policy"}])  # malformed -> eval raises

    # Force evaluate_for_span to raise by passing junk policies; the evaluator
    # should catch and, fail-closed, return block.

    def boom(*a, **k):
        raise RuntimeError("policy engine down")

    monkeypatch.setattr("policies.evaluate_for_span", boom)
    verdict = gw._evaluate("any_tool", {})
    assert verdict["action"] == "block"
    assert verdict["policy_name"] == "_fail_closed"


def test_fail_open_allows_on_eval_error_when_opted_in(monkeypatch):
    gw = _make_gateway(policies=[{"bad": "policy"}], fail_open=True)

    def boom(*a, **k):
        raise RuntimeError("policy engine down")

    monkeypatch.setattr("policies.evaluate_for_span", boom)
    verdict = gw._evaluate("any_tool", {})
    assert verdict["action"] == "allow"
    assert verdict.get("degraded") is True


def test_require_approval_returns_approval_error(monkeypatch):
    policies = [{
        "id": uuid.uuid4(),
        "name": "approve_transfers",
        "enabled": True,
        "action": "require_approval",
        "applies_to": [],
        "match_expression": 'attrs["strathon.tool.name"] == "wire_transfer"',
        "priority": 100,
    }]
    gw = _make_gateway(policies=policies)

    async def fake_forward(req):
        raise AssertionError("approval-gated call must not forward")

    monkeypatch.setattr(gw, "_forward", fake_forward)
    resp = asyncio.run(gw.handle_request(_tool_call("wire_transfer")))
    assert resp["error"]["code"] == -32041


def test_response_credential_scanning_redacts(monkeypatch):
    gw = _make_gateway(policies=[], scan_responses=True)
    leaked = "here is the key AKIAIOSFODNN7EXAMPLE for access"

    async def fake_forward(req):
        return {
            "jsonrpc": "2.0", "id": req.get("id"),
            "result": {"content": [{"type": "text", "text": leaked}]},
        }

    monkeypatch.setattr(gw, "_forward", fake_forward)
    resp = asyncio.run(gw.handle_request(_tool_call("read_secret")))
    text = resp["result"]["content"][0]["text"]
    assert "AKIAIOSFODNN7EXAMPLE" not in text


def test_tools_list_filters_blocked(monkeypatch):
    gw = _make_gateway(policies=[], blocked_tools=["evil"])

    async def fake_forward(req):
        return {
            "jsonrpc": "2.0", "id": req.get("id"),
            "result": {"tools": [{"name": "evil"}, {"name": "good"}]},
        }

    monkeypatch.setattr(gw, "_forward", fake_forward)
    resp = asyncio.run(gw.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    ))
    names = [t["name"] for t in resp["result"]["tools"]]
    assert "evil" not in names and "good" in names


# --------------------------------------------------------------------------
# Layer 2: API route tests (DB + auth)
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    db_url = os.getenv(
        "DATABASE_URL", "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon"
    )
    os.environ["DATABASE_URL"] = db_url
    try:
        psycopg.connect(db_url, autocommit=True).close()
    except Exception:
        pytest.skip("Postgres not reachable")
    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app) as c:
        yield c


def _mint(client, name, scopes):
    resp = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": name, "scopes": scopes},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


def test_mcp_proxy_requires_traces_write_scope(client):
    key = _mint(client, f"ro-{uuid.uuid4().hex[:8]}", ["policies:read"])
    resp = client.post(
        "/v1/mcp/proxy",
        headers={"Authorization": f"Bearer {key}"},
        json={"upstream_url": "http://upstream.invalid/mcp",
              "request": _tool_call("x")},
    )
    assert resp.status_code == 403
    assert "traces:write" in resp.json()["detail"]


def test_mcp_proxy_blocks_via_real_policy(client, monkeypatch):
    # Create a block policy for a uniquely-named tool, then proxy a call to it.
    tool = f"danger_{uuid.uuid4().hex[:8]}"
    create = client.post(
        "/v1/policies",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={
            "name": f"block_{tool}",
            "match_expression": f'attrs["strathon.tool.name"] == "{tool}"',
            "action": "block",
        },
    )
    assert create.status_code in (200, 201), create.text

    # Patch the gateway's upstream forward so the test never hits the network.
    import mcp_gateway

    async def fake_forward(self, req):
        raise AssertionError("blocked call must not forward upstream")

    monkeypatch.setattr(mcp_gateway.MCPSecurityGateway, "_forward", fake_forward)

    key = _mint(client, f"rw-{uuid.uuid4().hex[:8]}", ["traces:write"])
    resp = client.post(
        "/v1/mcp/proxy",
        headers={"Authorization": f"Bearer {key}"},
        json={"upstream_url": "http://upstream.invalid/mcp",
              "request": _tool_call(tool)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" in body and body["error"]["code"] == -32040


def test_shadow_block_policy_forwards(monkeypatch):
    """A shadow block policy is a dry run: the gateway must forward the
    call, not enforce. Enforcing a shadow policy would block live traffic
    during what the operator believes is a test."""
    policies = [{
        "id": uuid.uuid4(),
        "name": "shadow_block_send_email",
        "enabled": True,
        "shadow": True,
        "action": "block",
        "applies_to": [],
        "match_expression": 'attrs["strathon.tool.name"] == "send_email"',
        "priority": 100,
    }]
    gw = _make_gateway(policies=policies)
    forwarded = {}

    async def fake_forward(req):
        forwarded["called"] = True
        return {"jsonrpc": "2.0", "id": req.get("id"), "result": {"ok": True}}

    monkeypatch.setattr(gw, "_forward", fake_forward)
    resp = asyncio.run(gw.handle_request(_tool_call("send_email")))
    assert forwarded.get("called"), "shadow policy enforced at the gateway"
    assert "error" not in resp


def test_shadow_policy_does_not_mask_enabled_policy_at_gateway(monkeypatch):
    """A higher-priority shadow allow must not short-circuit a real block."""
    policies = [
        {
            "id": uuid.uuid4(),
            "name": "shadow_allow_all",
            "enabled": True,
            "shadow": True,
            "action": "allow",
            "applies_to": [],
            "match_expression": "true",
            "priority": 1000,
        },
        {
            "id": uuid.uuid4(),
            "name": "block_send_email",
            "enabled": True,
            "action": "block",
            "applies_to": [],
            "match_expression": 'attrs["strathon.tool.name"] == "send_email"',
            "priority": 100,
        },
    ]
    gw = _make_gateway(policies=policies)

    async def fake_forward(req):  # must NOT be called
        raise AssertionError("blocked tool call must not forward")

    monkeypatch.setattr(gw, "_forward", fake_forward)
    resp = asyncio.run(gw.handle_request(_tool_call("send_email")))
    assert "error" in resp
    assert resp["error"]["code"] == -32040

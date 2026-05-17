"""Tests for policy templates."""

from __future__ import annotations

import os
import uuid

import pytest


DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"


@pytest.fixture(scope="module")
def client():
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon",
    )
    os.environ["DATABASE_URL"] = db_url
    import psycopg
    try:
        psycopg.connect(db_url, autocommit=True).close()
    except Exception:
        pytest.skip("Postgres not reachable")
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        yield c


def _mint(client, scopes):
    resp = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": f"tpl-{uuid.uuid4().hex[:6]}", "scopes": scopes},
    )
    return resp.json()["key"]


def test_list_templates(client):
    key = _mint(client, ["policies:read"])
    resp = client.get(
        "/v1/policy-templates",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) >= 8
    assert all("id" in t for t in data)
    assert all("match_expression" in t for t in data)


def test_list_templates_filter_by_tag(client):
    key = _mint(client, ["policies:read"])
    resp = client.get(
        "/v1/policy-templates?tag=security",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) >= 1
    assert all("security" in t["tags"] for t in data)


def test_get_template(client):
    key = _mint(client, ["policies:read"])
    resp = client.get(
        "/v1/policy-templates/block-dangerous-tools",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "block-dangerous-tools"
    assert "ASI-02" in resp.json()["owasp_risks"]


def test_get_template_not_found(client):
    key = _mint(client, ["policies:read"])
    resp = client.get(
        "/v1/policy-templates/nonexistent",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404


def test_apply_template(client):
    key = _mint(client, ["policies:read", "policies:write"])
    resp = client.post(
        "/v1/policy-templates/block-dangerous-tools/apply",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["template_id"] == "block-dangerous-tools"
    assert "policy" in body
    assert body["policy"]["action"] == "block"
    # Cleanup.
    client.delete(
        f"/v1/policies/{body['policy']['id']}",
        headers={"Authorization": f"Bearer {key}"},
    )


def test_apply_template_requires_write(client):
    key = _mint(client, ["policies:read"])
    resp = client.post(
        "/v1/policy-templates/block-dangerous-tools/apply",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403


def test_apply_template_not_found(client):
    key = _mint(client, ["policies:read", "policies:write"])
    resp = client.post(
        "/v1/policy-templates/nonexistent/apply",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404


def test_apply_throttle_template(client):
    key = _mint(client, ["policies:read", "policies:write"])
    resp = client.post(
        "/v1/policy-templates/throttle-expensive-models/apply",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["policy"]["action"] == "throttle"
    assert body["policy"]["action_config"]["max_calls"] == 10
    # Cleanup.
    client.delete(
        f"/v1/policies/{body['policy']['id']}",
        headers={"Authorization": f"Bearer {key}"},
    )

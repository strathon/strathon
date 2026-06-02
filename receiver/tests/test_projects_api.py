"""Tests for project management endpoints."""

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


def test_create_project(client):
    slug = f"test-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/v1/projects",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": f"Test {slug}", "slug": slug},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["slug"] == slug
    assert body["api_key"].startswith("stra_")
    assert "id" in body


def test_create_project_duplicate_slug(client):
    slug = f"dup-{uuid.uuid4().hex[:8]}"
    client.post(
        "/v1/projects",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": "First", "slug": slug},
    )
    resp = client.post(
        "/v1/projects",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": "Second", "slug": slug},
    )
    assert resp.status_code == 409


def test_create_project_bad_slug(client):
    resp = client.post(
        "/v1/projects",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": "Bad", "slug": "AB!!"},
    )
    assert resp.status_code == 400


def test_list_projects(client):
    resp = client.get(
        "/v1/projects",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert isinstance(data, list)
    # At least the seeded default project.
    assert any(p["slug"] == "default" for p in data)


def test_get_project(client):
    resp = client.get(
        "/v1/projects/default",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
    )
    assert resp.status_code == 200
    assert resp.json()["slug"] == "default"
    assert "resource_counts" in resp.json()


def test_get_project_not_found(client):
    resp = client.get(
        "/v1/projects/nonexistent-slug",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
    )
    assert resp.status_code == 404


def test_update_project(client):
    slug = f"upd-{uuid.uuid4().hex[:8]}"
    client.post(
        "/v1/projects",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": "Original", "slug": slug},
    )
    resp = client.patch(
        f"/v1/projects/{slug}",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": "Renamed"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"


def test_delete_project(client):
    slug = f"del-{uuid.uuid4().hex[:8]}"
    client.post(
        "/v1/projects",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": "Delete Me", "slug": slug},
    )
    resp = client.delete(
        f"/v1/projects/{slug}",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
    )
    assert resp.status_code == 204
    # Should not appear in list.
    resp = client.get(
        f"/v1/projects/{slug}",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
    )
    assert resp.status_code == 404


def test_requires_projects_manage_scope(client):
    # Mint a key with only traces:read — should be rejected.
    resp = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": f"no-manage-{uuid.uuid4().hex[:6]}", "scopes": ["traces:read"]},
    )
    key = resp.json()["key"]
    resp = client.get(
        "/v1/projects",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403


def test_delete_non_last_project_succeeds(client):
    h = {"Authorization": f"Bearer {DEV_KEY}"}
    slug = f"del-ok-{uuid.uuid4().hex[:6]}"
    r = client.post("/v1/projects", headers=h, json={"name": "Del OK", "slug": slug})
    assert r.status_code == 201
    # At least the default project plus this one exist, so deleting this is allowed.
    r = client.delete(f"/v1/projects/{slug}", headers=h)
    assert r.status_code == 204


def test_cannot_delete_last_project(client):
    h = {"Authorization": f"Bearer {DEV_KEY}"}
    # Soft-delete every project except one, then assert the final delete is blocked.
    active = [p["slug"] for p in client.get("/v1/projects", headers=h).json()["data"]
              if not p.get("deleted_at")]
    for s in active[1:]:
        client.delete(f"/v1/projects/{s}", headers=h)
    remaining = [p["slug"] for p in client.get("/v1/projects", headers=h).json()["data"]
                 if not p.get("deleted_at")]
    assert len(remaining) == 1
    r = client.delete(f"/v1/projects/{remaining[0]}", headers=h)
    assert r.status_code == 409
    assert "last project" in r.json()["detail"].lower()

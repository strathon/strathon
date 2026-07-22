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


def test_delete_project_revokes_its_api_keys(client):
    """A deleted project's keys must stop authenticating (fail closed).

    Project create auto-mints a '{slug}-default-key'. After the project is
    soft-deleted, that key must be revoked -- otherwise 'delete' leaves a
    live credential that can keep ingesting spans into and reading data out
    of a project the operator believes is gone.
    """
    slug = f"delkey-{uuid.uuid4().hex[:8]}"
    r = client.post(
        "/v1/projects",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": "Del Key", "slug": slug},
    )
    assert r.status_code == 201
    project_key = r.json()["api_key"]

    # The freshly minted key authenticates before deletion.
    r = client.get("/v1/policies", headers={"Authorization": f"Bearer {project_key}"})
    assert r.status_code == 200

    r = client.delete(
        f"/v1/projects/{slug}",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
    )
    assert r.status_code == 204

    # And is rejected after: the delete revoked every live key of the project.
    r = client.get("/v1/policies", headers={"Authorization": f"Bearer {project_key}"})
    assert r.status_code == 401


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
    # Soft-delete every project EXCEPT the seeded default, then assert the
    # final delete is blocked. The survivor must be 'default' specifically:
    # DEV_KEY and every other module's fixtures live in that project, so
    # keeping an arbitrary survivor (as this test previously did with
    # active[1:]) soft-deleted the shared bootstrap project and poisoned
    # every spans/analytics/simulate module that ran after it.
    active = [p["slug"] for p in client.get("/v1/projects", headers=h).json()["data"]
              if not p.get("deleted_at")]
    for s in active:
        if s != "default":
            client.delete(f"/v1/projects/{s}", headers=h)
    remaining = [p["slug"] for p in client.get("/v1/projects", headers=h).json()["data"]
                 if not p.get("deleted_at")]
    assert remaining == ["default"]
    r = client.delete("/v1/projects/default", headers=h)
    assert r.status_code == 409
    assert "last project" in r.json()["detail"].lower()

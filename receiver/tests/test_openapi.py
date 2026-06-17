"""Tests for OpenAPI spec generation."""

from __future__ import annotations

import os

import pytest


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


def test_openapi_json_accessible(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert spec["info"]["title"] == "Strathon Receiver"
    assert spec["info"]["version"] == "1.2.1"
    assert "paths" in spec
    # Key endpoints present.
    assert "/v1/traces" in spec["paths"]
    assert "/v1/policies" in spec["paths"]
    assert "/v1/spans" in spec["paths"]


def test_openapi_has_tags(client):
    r = client.get("/openapi.json")
    spec = r.json()
    tag_names = [t["name"] for t in spec.get("tags", [])]
    assert "health" in tag_names
    assert "policies" in tag_names
    assert "analytics" in tag_names
    assert "audit" in tag_names


def test_docs_accessible(client):
    r = client.get("/docs")
    assert r.status_code == 200


def test_redoc_accessible(client):
    r = client.get("/redoc")
    assert r.status_code == 200

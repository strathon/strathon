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
    assert spec["info"]["version"] == "1.3.0"
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


def test_no_duplicate_route_registrations():
    """No path+method may be registered by two routers.

    FastAPI serves whichever router was included first and silently ignores
    the rest, so a second registration is dead code that becomes live if the
    include_router order in main.py ever changes. A convenience alias for
    /v1/auth/change-password shadowed the session-authenticated handler this
    way, and it changed passwords on a read scope without invalidating other
    sessions.

    The generated OpenAPI schema cannot detect this: two registrations of one
    path collapse into a single dict entry. FastAPI's own Duplicate Operation
    ID warning only fires when the handler function names collide too. So walk
    the real routes, through the _IncludedRouter wrappers that include_router
    leaves in app.routes.
    """
    from main import app

    def walk(routes):
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                yield from walk(original.routes)
            elif getattr(route, "path", None):
                yield route

    seen: dict[tuple[str, str], list[str]] = {}
    for route in walk(app.routes):
        for method in getattr(route, "methods", None) or []:
            if method in ("HEAD", "OPTIONS"):
                continue
            seen.setdefault((method, route.path), []).append(route.endpoint.__module__)

    duplicates = {k: v for k, v in seen.items() if len(v) > 1}
    assert not duplicates, f"duplicate route registrations: {duplicates}"

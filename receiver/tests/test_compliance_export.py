"""Tests for EU AI Act compliance evidence export."""

from __future__ import annotations

import os
import sys

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)

DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(scope="module")
def client():
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon",
    )
    os.environ["DATABASE_URL"] = db_url
    try:
        import psycopg
        conn = psycopg.connect(db_url, autocommit=True)
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable")

    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app) as c:
        yield c


def test_compliance_export_returns_200(client):
    resp = client.post("/v1/compliance/export", headers=_auth(DEV_KEY), json={})
    assert resp.status_code == 200
    data = resp.json()
    assert "articles" in data
    assert "recommendations" in data
    assert "generated_at" in data
    assert "framework" in data


def test_compliance_export_has_all_articles(client):
    resp = client.post("/v1/compliance/export", headers=_auth(DEV_KEY), json={})
    articles = resp.json()["articles"]
    expected = [
        "article_9_risk_management",
        "article_11_technical_documentation",
        "article_12_event_logging",
        "article_14_human_oversight",
        "article_15_robustness",
        "article_19_retention",
    ]
    for key in expected:
        assert key in articles, f"Missing article section: {key}"


def test_compliance_article_sections_have_description(client):
    resp = client.post("/v1/compliance/export", headers=_auth(DEV_KEY), json={})
    articles = resp.json()["articles"]
    for key, section in articles.items():
        assert "description" in section, f"{key} missing description"
        assert "compliant" in section, f"{key} missing compliant flag"


def test_compliance_recommendations_are_strings(client):
    resp = client.post("/v1/compliance/export", headers=_auth(DEV_KEY), json={})
    data = resp.json()
    assert isinstance(data["recommendations"], list)
    for r in data["recommendations"]:
        assert isinstance(r, str)


def test_compliance_requires_auth(client):
    resp = client.post("/v1/compliance/export", json={})
    assert resp.status_code == 401

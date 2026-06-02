"""Tests for the general data-export endpoint (POST /v1/export).

This is the manual, on-demand export of a project's own data as JSON or a
ZIP of per-dataset CSVs. These tests lock in the contract: auth, dataset
validation, format validation, and the shape of each output.
"""

from __future__ import annotations

import io
import os
import zipfile

import pytest

DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"


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


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {DEV_KEY}"}


def test_export_requires_auth(client):
    r = client.post("/v1/export", json={"datasets": ["policies"]})
    assert r.status_code == 401


def test_export_rejects_empty_datasets(client):
    r = client.post("/v1/export", headers=_auth(), json={"datasets": []})
    assert r.status_code == 400


def test_export_rejects_unknown_dataset(client):
    r = client.post(
        "/v1/export", headers=_auth(),
        json={"datasets": ["policies", "not_a_dataset"]},
    )
    assert r.status_code == 400


def test_export_rejects_bad_format(client):
    r = client.post(
        "/v1/export", headers=_auth(),
        json={"datasets": ["policies"], "format": "xml"},
    )
    assert r.status_code == 400


def test_export_json_returns_document(client):
    r = client.post(
        "/v1/export", headers=_auth(),
        json={"datasets": ["policies", "audit"], "time_range": "30d", "format": "json"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "attachment" in r.headers.get("content-disposition", "")
    body = r.json()
    assert body["time_range"] == "30d"
    assert "policies" in body["datasets"]
    assert "audit" in body["datasets"]
    assert isinstance(body["datasets"]["policies"], list)


def test_export_csv_returns_zip_of_csvs(client):
    r = client.post(
        "/v1/export", headers=_auth(),
        json={"datasets": ["policies", "audit"], "format": "csv"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "policies.csv" in names
    assert "audit.csv" in names
    assert "manifest.json" in names


def test_export_compliance_dataset_included_as_package(client):
    """The compliance dataset is a generated package, not a row set; in CSV
    mode it lands as compliance.json inside the zip."""
    r = client.post(
        "/v1/export", headers=_auth(),
        json={"datasets": ["compliance"], "format": "csv"},
    )
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert "compliance.json" in zf.namelist()

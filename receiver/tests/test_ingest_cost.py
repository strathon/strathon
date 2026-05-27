"""End-to-end tests for cost computation in the ingest path.

These tests drive the real OTLP ingest endpoint with payloads that
carry gen_ai.usage.* and gen_ai.request.model attributes, then verify
the persisted spans.cost_usd column has the correct value.

Coverage:
  * Known model + tokens -> cost_usd is computed and persisted
  * Unknown model -> cost_usd is NULL (NOT 0)
  * Missing tokens -> cost_usd is NULL
  * SDK-supplied strathon.agent.cost.usd takes precedence over our
    catalog lookup (provider-specific pricing the SDK has access to)
  * Per-project price overrides supersede the catalog
  * Tool spans (no LLM model) have cost_usd NULL
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from decimal import Decimal

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)


DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"
DEFAULT_PROJECT_ID = "00000000-0000-0000-0000-000000000001"


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


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _build_otlp(*, trace_id: bytes, span_id: bytes, attrs: dict, name: str = "llm.generate") -> bytes:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
    from opentelemetry.proto.trace.v1.trace_pb2 import (
        ResourceSpans, ScopeSpans, Span,
    )

    def _kv(k: str, v):
        if isinstance(v, bool):
            return KeyValue(key=k, value=AnyValue(bool_value=v))
        if isinstance(v, int):
            return KeyValue(key=k, value=AnyValue(int_value=v))
        if isinstance(v, float):
            return KeyValue(key=k, value=AnyValue(double_value=v))
        if isinstance(v, str):
            return KeyValue(key=k, value=AnyValue(string_value=v))
        return KeyValue(key=k, value=AnyValue(string_value=json.dumps(v)))

    span = Span(
        trace_id=trace_id,
        span_id=span_id,
        name=name,
        kind=Span.SPAN_KIND_CLIENT,
        start_time_unix_nano=1_700_000_000_000_000_000,
        end_time_unix_nano=1_700_000_001_000_000_000,
        attributes=[_kv(k, v) for k, v in attrs.items()],
    )
    req = ExportTraceServiceRequest(
        resource_spans=[ResourceSpans(scope_spans=[ScopeSpans(spans=[span])])],
    )
    return req.SerializeToString()


def _read_cost(span_id_bytes: bytes) -> Decimal | None:
    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    try:
        row = conn.execute(
            "SELECT cost_usd FROM spans WHERE span_id = %s::bytea",
            (span_id_bytes,),
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _cleanup_span(span_id_bytes: bytes) -> None:
    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    try:
        # Get the trace_id so we can delete the trace too (CASCADE handles span)
        row = conn.execute(
            "SELECT trace_id FROM spans WHERE span_id = %s::bytea",
            (span_id_bytes,),
        ).fetchone()
        if row is not None:
            conn.execute("DELETE FROM traces WHERE id = %s::bytea", (row[0],))
    finally:
        conn.close()


def _cleanup_override(model_name: str) -> None:
    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    try:
        conn.execute(
            "DELETE FROM model_price_overrides WHERE model_name = %s",
            (model_name,),
        )
    finally:
        conn.close()


def _post_otlp(client, payload: bytes) -> None:
    r = client.post(
        "/v1/traces",
        headers={**_auth(DEV_KEY), "Content-Type": "application/x-protobuf"},
        content=payload,
    )
    assert r.status_code == 200, r.text


# ---- Tests ----------------------------------------------------------


def test_known_model_with_tokens_computes_cost(client):
    """gpt-4o with 1000 input + 500 output tokens:
       1000 * 0.0000025 + 500 * 0.00001 = 0.0075
    """
    trace_id = uuid.uuid4().bytes
    span_id = uuid.uuid4().bytes[:8]

    payload = _build_otlp(
        trace_id=trace_id,
        span_id=span_id,
        attrs={
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 1000,
            "gen_ai.usage.output_tokens": 500,
        },
    )
    try:
        _post_otlp(client, payload)
        cost = _read_cost(span_id)
        assert cost is not None
        assert cost == Decimal("0.007500")
    finally:
        _cleanup_span(span_id)


def test_unknown_model_persists_null_cost(client):
    """Unknown model -> NULL, NOT 0. A 0 would silently misattribute."""
    trace_id = uuid.uuid4().bytes
    span_id = uuid.uuid4().bytes[:8]
    payload = _build_otlp(
        trace_id=trace_id,
        span_id=span_id,
        attrs={
            "gen_ai.request.model": "fictional-llm-v9-pro",
            "gen_ai.usage.input_tokens": 1000,
            "gen_ai.usage.output_tokens": 500,
        },
    )
    try:
        _post_otlp(client, payload)
        cost = _read_cost(span_id)
        assert cost is None, f"unknown model should produce NULL, got {cost}"
    finally:
        _cleanup_span(span_id)


def test_missing_tokens_persists_null_cost(client):
    trace_id = uuid.uuid4().bytes
    span_id = uuid.uuid4().bytes[:8]
    payload = _build_otlp(
        trace_id=trace_id,
        span_id=span_id,
        attrs={"gen_ai.request.model": "gpt-4o"},
        # no tokens
    )
    try:
        _post_otlp(client, payload)
        cost = _read_cost(span_id)
        assert cost is None
    finally:
        _cleanup_span(span_id)


def test_sdk_supplied_cost_takes_precedence(client):
    """If the SDK already set strathon.agent.cost.usd (e.g. it has
    provider-specific pricing including cache-hit discounts), we
    trust it over our catalog math."""
    trace_id = uuid.uuid4().bytes
    span_id = uuid.uuid4().bytes[:8]
    payload = _build_otlp(
        trace_id=trace_id,
        span_id=span_id,
        attrs={
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 1000,
            "gen_ai.usage.output_tokens": 500,
            # Catalog math says $0.0075; SDK says $0.001 (cache-discounted)
            "strathon.agent.cost.usd": 0.001,
        },
    )
    try:
        _post_otlp(client, payload)
        cost = _read_cost(span_id)
        # SDK's value wins
        assert cost == Decimal("0.001000")
    finally:
        _cleanup_span(span_id)


def test_project_override_supersedes_catalog(client):
    """Setting an override via the API should change cost computation
    for subsequent ingests."""
    trace_id = uuid.uuid4().bytes
    span_id = uuid.uuid4().bytes[:8]

    # Install an override that makes gpt-4o 4x more expensive
    r = client.post(
        "/v1/model_prices",
        headers=_auth(DEV_KEY),
        json={
            "model_name": "gpt-4o",
            "input_cost_per_token": "0.00001",   # was 0.0000025
            "output_cost_per_token": "0.00004",  # was 0.00001
        },
    )
    assert r.status_code == 200

    try:
        payload = _build_otlp(
            trace_id=trace_id,
            span_id=span_id,
            attrs={
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.usage.input_tokens": 1000,
                "gen_ai.usage.output_tokens": 500,
            },
        )
        _post_otlp(client, payload)
        cost = _read_cost(span_id)
        # 1000 * 0.00001 + 500 * 0.00004 = 0.01 + 0.02 = 0.03
        assert cost == Decimal("0.030000")
    finally:
        _cleanup_span(span_id)
        _cleanup_override("gpt-4o")


def test_tool_span_has_null_cost(client):
    """A tool span (gen_ai.tool.name, no model) shouldn't have cost."""
    trace_id = uuid.uuid4().bytes
    span_id = uuid.uuid4().bytes[:8]
    payload = _build_otlp(
        trace_id=trace_id,
        span_id=span_id,
        name="tool.send_email",
        attrs={
            "gen_ai.tool.name": "send_email",
            # No request.model, no usage tokens
        },
    )
    try:
        _post_otlp(client, payload)
        cost = _read_cost(span_id)
        assert cost is None
    finally:
        _cleanup_span(span_id)


def test_claude_haiku_cost_math(client):
    """100 input + 200 output on claude-3-5-haiku-20241022:
       100 * 0.0000008 + 200 * 0.000004 = 0.00008 + 0.0008 = 0.00088
    """
    trace_id = uuid.uuid4().bytes
    span_id = uuid.uuid4().bytes[:8]
    payload = _build_otlp(
        trace_id=trace_id,
        span_id=span_id,
        attrs={
            "gen_ai.request.model": "claude-3-5-haiku-20241022",
            "gen_ai.usage.input_tokens": 100,
            "gen_ai.usage.output_tokens": 200,
        },
    )
    try:
        _post_otlp(client, payload)
        cost = _read_cost(span_id)
        assert cost == Decimal("0.000880")
    finally:
        _cleanup_span(span_id)

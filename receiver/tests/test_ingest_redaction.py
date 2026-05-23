"""End-to-end tests for the redaction wiring in api/traces.py.

These tests drive the real ingest endpoint with OTLP protobuf payloads
containing PII, then verify:

  1. The persisted span row has the PII redacted.
  2. Policy match expressions that reference the UNREDACTED content
     still fire (the critical property — redaction must not break
     the firewall semantics).
  3. The webhook delivery payload, if a matching alert policy is
     present, also carries the redacted content (no PII leaves the
     receiver in any downstream artifact).
  4. With redaction disabled per-project, raw content survives.

We seed projects + settings + policies via SQL so each test runs in
isolation. The ingest endpoint hits the same code path the SDK uses
in production.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

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


def _build_otlp_payload(
    *,
    trace_id: bytes,
    span_id: bytes,
    span_name: str,
    attrs: dict,
):
    """Construct a minimal OTLP protobuf payload with one span."""
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )
    from opentelemetry.proto.common.v1.common_pb2 import (
        AnyValue, KeyValue,
    )
    from opentelemetry.proto.trace.v1.trace_pb2 import (
        ResourceSpans, ScopeSpans, Span,
    )

    def _to_kv(k: str, v):
        if isinstance(v, str):
            return KeyValue(key=k, value=AnyValue(string_value=v))
        if isinstance(v, bool):
            return KeyValue(key=k, value=AnyValue(bool_value=v))
        if isinstance(v, int):
            return KeyValue(key=k, value=AnyValue(int_value=v))
        if isinstance(v, float):
            return KeyValue(key=k, value=AnyValue(double_value=v))
        # JSONify anything else
        return KeyValue(key=k, value=AnyValue(string_value=json.dumps(v)))

    span = Span(
        trace_id=trace_id,
        span_id=span_id,
        name=span_name,
        kind=Span.SPAN_KIND_INTERNAL,
        start_time_unix_nano=1_700_000_000_000_000_000,
        end_time_unix_nano=1_700_000_001_000_000_000,
        attributes=[_to_kv(k, v) for k, v in attrs.items()],
    )
    req = ExportTraceServiceRequest(
        resource_spans=[ResourceSpans(scope_spans=[ScopeSpans(spans=[span])])],
    )
    return req.SerializeToString()


@pytest.fixture
def disable_redaction(client):
    """Toggle the default project's redaction OFF for tests that want
    to verify the disabled-path doesn't redact, then restore."""
    import psycopg
    db_url = os.environ["DATABASE_URL"]
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        conn.execute(
            "UPDATE project_settings SET pii_redaction_enabled = false "
            "WHERE project_id = %s::uuid",
            (DEFAULT_PROJECT_ID,),
        )
        yield
    finally:
        conn.execute(
            "UPDATE project_settings SET pii_redaction_enabled = true "
            "WHERE project_id = %s::uuid",
            (DEFAULT_PROJECT_ID,),
        )
        conn.close()


def _read_persisted_attrs(span_id_hex: str) -> dict | None:
    """Pull the attributes column for one span_id via raw SQL.

    We use psycopg directly rather than the FastAPI session because the
    TestClient's transaction has already committed by the time we read,
    and we want a fresh view of the table. Returns None when no row
    matches; callers either assert presence or check explicitly.
    """
    import psycopg
    db_url = os.environ["DATABASE_URL"]
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        row = conn.execute(
            "SELECT attributes FROM spans WHERE span_id = %s::bytea",
            (bytes.fromhex(span_id_hex),),
        ).fetchone()
        if row is None:
            return None
        # JSONB comes back as a dict directly
        return row[0]
    finally:
        conn.close()


# ---- Core property: redaction happens at persistence -------------------


def test_email_in_tool_args_redacted_in_persisted_span(client):
    """Send a span whose strathon.tool.args contains an email; verify
    the persisted row carries [EMAIL_ADDRESS], not the raw email."""
    trace_id = os.urandom(16)
    span_id = os.urandom(8)
    payload = _build_otlp_payload(
        trace_id=trace_id, span_id=span_id,
        span_name="langgraph.tool.send_email",
        attrs={
            "gen_ai.tool.name": "send_email",
            "strathon.tool.args": '{"to": "alice@example.com", "body": "hi"}',
        },
    )
    resp = client.post(
        "/v1/traces",
        headers={**_auth(DEV_KEY), "Content-Type": "application/x-protobuf"},
        content=payload,
    )
    assert resp.status_code == 200, resp.text

    persisted = _read_persisted_attrs(span_id.hex())
    assert persisted is not None
    args_str = persisted.get("strathon.tool.args", "")
    assert "[EMAIL_ADDRESS]" in args_str
    assert "alice@example.com" not in args_str


def test_credit_card_redacted_with_luhn_filter(client):
    """A valid Luhn number is redacted; an invalid one passes through."""
    trace_id = os.urandom(16)
    span_id = os.urandom(8)
    payload = _build_otlp_payload(
        trace_id=trace_id, span_id=span_id,
        span_name="op",
        attrs={
            # Valid Luhn (4242 4242 4242 4242), in mixed with an
            # invalid 16-digit number that should pass through.
            "strathon.tool.args": "card 4242 4242 4242 4242 ref 1111111111111111",
        },
    )
    resp = client.post(
        "/v1/traces",
        headers={**_auth(DEV_KEY), "Content-Type": "application/x-protobuf"},
        content=payload,
    )
    assert resp.status_code == 200

    persisted = _read_persisted_attrs(span_id.hex())
    args_str = persisted["strathon.tool.args"]
    assert "[CREDIT_CARD]" in args_str
    assert "4242" not in args_str
    # Invalid Luhn 16-digit number left alone
    assert "1111111111111111" in args_str


def test_api_key_redacted_in_persisted_span(client):
    """sk-... is the highest-impact pattern; verify end-to-end."""
    trace_id = os.urandom(16)
    span_id = os.urandom(8)
    payload = _build_otlp_payload(
        trace_id=trace_id, span_id=span_id,
        span_name="op",
        attrs={
            "strathon.tool.args": "env OPENAI_API_KEY=sk-abcd1234567890efghijklmnop",
        },
    )
    resp = client.post(
        "/v1/traces",
        headers={**_auth(DEV_KEY), "Content-Type": "application/x-protobuf"},
        content=payload,
    )
    assert resp.status_code == 200
    persisted = _read_persisted_attrs(span_id.hex())
    args_str = persisted["strathon.tool.args"]
    assert "REDACTED" in args_str
    assert "sk-abcd" not in args_str


def test_redaction_disabled_passes_pii_through(client, disable_redaction):
    """When pii_redaction_enabled=false the ingest is a pure passthrough."""
    trace_id = os.urandom(16)
    span_id = os.urandom(8)
    payload = _build_otlp_payload(
        trace_id=trace_id, span_id=span_id,
        span_name="op",
        attrs={"strathon.tool.args": "send to alice@example.com"},
    )
    resp = client.post(
        "/v1/traces",
        headers={**_auth(DEV_KEY), "Content-Type": "application/x-protobuf"},
        content=payload,
    )
    assert resp.status_code == 200
    persisted = _read_persisted_attrs(span_id.hex())
    # Raw email survives — the operator opted into this
    assert "alice@example.com" in persisted["strathon.tool.args"]
    assert "[EMAIL_ADDRESS]" not in persisted["strathon.tool.args"]


# ---- THE critical property: policy match runs on unredacted content ---


def test_policy_match_expression_sees_unredacted_content(client):
    """A policy whose match_expression references the raw email must
    still fire even though the email is redacted on persistence.

    This is the killer property: redaction is for storage, not for
    matching. If we ran redaction BEFORE policy evaluation, the
    expression `attrs["strathon.tool.args"].contains("@competitor.com")`
    would never match and the firewall would be silently broken.
    """
    import psycopg
    db_url = os.environ["DATABASE_URL"]
    policy_id = str(uuid.uuid4())
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        conn.execute(
            """INSERT INTO policies (id, project_id, name, description,
                                     match_expression, action, action_config,
                                     applies_to, enabled, priority)
               VALUES (%s::uuid, %s::uuid, %s, '',
                       'attrs["strathon.tool.args"].contains("@competitor.com")',
                       'log', '{}'::jsonb, ARRAY[]::TEXT[], true, 0)""",
            (policy_id, DEFAULT_PROJECT_ID, f"redact_test_{policy_id[:8]}"),
        )

        trace_id = os.urandom(16)
        span_id = os.urandom(8)
        payload = _build_otlp_payload(
            trace_id=trace_id, span_id=span_id,
            span_name="langgraph.tool.send_email",
            attrs={
                "strathon.tool.args": '{"to": "rival@competitor.com"}',
            },
        )
        resp = client.post(
            "/v1/traces",
            headers={**_auth(DEV_KEY), "Content-Type": "application/x-protobuf"},
            content=payload,
        )
        assert resp.status_code == 200

        # 1. The policy_matches table should have one row from this span
        match_row = conn.execute(
            "SELECT policy_id, action FROM policy_matches WHERE span_id = %s::bytea",
            (span_id,),
        ).fetchone()
        assert match_row is not None, (
            "policy_match was not recorded — redaction likely fired BEFORE eval"
        )
        assert str(match_row[0]) == policy_id
        assert match_row[1] == "log"

        # 2. The persisted span MUST have redacted content (so neither
        # readers of spans nor downstream alert consumers see the PII)
        persisted = _read_persisted_attrs(span_id.hex())
        assert "[EMAIL_ADDRESS]" in persisted["strathon.tool.args"]
        assert "@competitor.com" not in persisted["strathon.tool.args"]
    finally:
        conn.execute("DELETE FROM policy_matches WHERE policy_id = %s::uuid",
                     (policy_id,))
        conn.execute("DELETE FROM policies WHERE id = %s::uuid", (policy_id,))
        conn.close()


# ---- Webhook payload carries redacted content -------------------------


def test_alert_webhook_payload_has_redacted_attrs(client):
    """An alert policy produces a webhook delivery row whose payload
    must carry the redacted attrs, not the raw ones."""
    import psycopg
    db_url = os.environ["DATABASE_URL"]
    policy_id = str(uuid.uuid4())
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        conn.execute(
            """INSERT INTO policies (id, project_id, name, description,
                                     match_expression, action, action_config,
                                     applies_to, enabled, priority)
               VALUES (%s::uuid, %s::uuid, %s, '', 'true',
                       'alert', %s::jsonb,
                       ARRAY[]::TEXT[], true, 0)""",
            (policy_id, DEFAULT_PROJECT_ID, f"alert_redact_{policy_id[:8]}",
             '{"webhook_url":"https://example.test/hook"}'),
        )

        trace_id = os.urandom(16)
        span_id = os.urandom(8)
        payload = _build_otlp_payload(
            trace_id=trace_id, span_id=span_id,
            span_name="op",
            attrs={
                "strathon.tool.args": '{"email": "leak@example.com"}',
            },
        )
        resp = client.post(
            "/v1/traces",
            headers={**_auth(DEV_KEY), "Content-Type": "application/x-protobuf"},
            content=payload,
        )
        assert resp.status_code == 200

        # The webhook delivery row was queued; its payload must carry
        # the redacted content.
        row = conn.execute(
            "SELECT payload FROM webhook_deliveries "
            "WHERE policy_id = %s::uuid LIMIT 1",
            (policy_id,),
        ).fetchone()
        assert row is not None, "no webhook delivery was queued"
        payload_json = row[0]
        # The whole payload is a dict; the attrs sub-dict has the
        # redacted tool args. Serialize for inspection.
        serialized = json.dumps(payload_json)
        assert "[EMAIL_ADDRESS]" in serialized
        assert "leak@example.com" not in serialized
    finally:
        conn.execute(
            "DELETE FROM webhook_deliveries WHERE policy_id = %s::uuid",
            (policy_id,),
        )
        conn.execute("DELETE FROM policy_matches WHERE policy_id = %s::uuid",
                     (policy_id,))
        conn.execute("DELETE FROM policies WHERE id = %s::uuid", (policy_id,))
        conn.close()

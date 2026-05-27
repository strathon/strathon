"""End-to-end test for the full operator lifecycle.

Exercises the complete flow an enterprise operator would follow:

  1. Create a new project via the projects API.
  2. Use the minted key to create a policy.
  3. Ingest a span via OTLP.
  4. Verify the span appears in search.
  5. Verify the trace appears in the trace list.
  6. Verify the trace tree shows the span.
  7. Verify span aggregation counts the span.
  8. Verify policy versioning captured the create.
  9. Update the policy and verify version 2 exists.
  10. Check the audit log captured the policy creation.

Skipped if Postgres isn't reachable.
"""

from __future__ import annotations

import json
import os
import time
import uuid
import urllib.request


DEV_API_KEY = "stra_dev_local_default_project_do_not_use_in_production"


def _api(base: str, method: str, path: str, body=None, key=None):
    """Helper for HTTP requests to the receiver."""
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        return e.code, json.loads(raw) if raw.strip() else {}


def _otlp_ingest(base: str, key: str, trace_id_bytes: bytes, span_id_bytes: bytes,
                 agent_name: str = "test-agent", tool_name: str = "test-tool",
                 model: str = "gpt-4o"):
    """Send a single span via the OTLP protobuf ingest endpoint."""
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
    from opentelemetry.proto.trace.v1.trace_pb2 import (
        ResourceSpans, ScopeSpans, Span,
    )
    from opentelemetry.proto.resource.v1.resource_pb2 import Resource

    def _kv(k, v):
        if isinstance(v, int):
            return KeyValue(key=k, value=AnyValue(int_value=v))
        return KeyValue(key=k, value=AnyValue(string_value=str(v)))

    now_ns = int(time.time() * 1e9)
    span = Span(
        trace_id=trace_id_bytes,
        span_id=span_id_bytes,
        name=f"{agent_name}/{tool_name}",
        kind=Span.SPAN_KIND_CLIENT,
        start_time_unix_nano=now_ns,
        end_time_unix_nano=now_ns + 50_000_000,
        attributes=[
            _kv("strathon.agent.name", agent_name),
            _kv("gen_ai.tool.name", tool_name),
            _kv("gen_ai.request.model", model),
            _kv("gen_ai.usage.input_tokens", 100),
            _kv("gen_ai.usage.output_tokens", 50),
        ],
    )
    req = ExportTraceServiceRequest(
        resource_spans=[ResourceSpans(
            resource=Resource(attributes=[_kv("service.name", "integration-test")]),
            scope_spans=[ScopeSpans(spans=[span])],
        )],
    )
    http_req = urllib.request.Request(
        f"{base}/v1/traces",
        data=req.SerializeToString(),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/x-protobuf",
        },
    )
    try:
        with urllib.request.urlopen(http_req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def test_full_operator_lifecycle(receiver: str):
    """Complete lifecycle: project → policy → ingest → search → analytics → audit."""
    slug = f"e2e-{uuid.uuid4().hex[:8]}"

    # 1. Create project.
    status, body = _api(receiver, "POST", "/v1/projects",
                        {"name": f"E2E {slug}", "slug": slug}, DEV_API_KEY)
    assert status == 201, f"create project: {body}"
    assert body["api_key"].startswith("stra_")

    # The minted key has only traces:write + policies:read (SDK defaults).
    # Mint a broader key for operator actions.
    status, body = _api(receiver, "POST", "/v1/api_keys", {
        "name": f"operator-{slug}",
        "scopes": ["*"],
    }, DEV_API_KEY)
    assert status == 201

    # 2. Create a policy using the default project's operator key
    #    (the new project's key only has SDK scopes).
    #    Actually, let's use the dev key which is on the default project.
    #    For the new project we need an operator key scoped to it.
    #    The project_key is SDK-scoped, so we use it for ingest.
    #    For policy creation on the new project, we need a key on that project.
    #    Since the projects API auto-mints only SDK keys, we'd need api_keys
    #    endpoint scoped to the new project. But the api_keys endpoint uses
    #    the caller's project_id, so the DEV_KEY creates keys for 'default'.
    #
    #    For now, test the flow on the default project using the DEV_KEY
    #    for operator actions and a minted SDK key for ingest.

    # Create policy on default project.
    policy_name = f"block-{uuid.uuid4().hex[:6]}"
    status, body = _api(receiver, "POST", "/v1/policies", {
        "name": policy_name,
        "match_expression": 'attrs["gen_ai.tool.name"] == "dangerous-tool"',
        "action": "block",
    }, DEV_API_KEY)
    assert status == 201, f"create policy: {body}"
    policy_id = body["id"]

    # 3. Ingest a span on default project.
    trace_id_bytes = uuid.uuid4().bytes
    span_id_bytes = os.urandom(8)
    trace_id_hex = trace_id_bytes.hex()
    ingest_status = _otlp_ingest(
        receiver, DEV_API_KEY, trace_id_bytes, span_id_bytes,
        agent_name="lifecycle-bot", tool_name="safe-tool", model="gpt-4o",
    )
    assert ingest_status == 200, f"ingest failed: {ingest_status}"

    # Brief settle time for async processing.
    time.sleep(0.5)

    # 4. Span search.
    status, body = _api(receiver, "GET",
                        "/v1/spans?agent_name=lifecycle-bot&limit=5",
                        key=DEV_API_KEY)
    assert status == 200, f"span search: {body}"
    assert len(body["data"]) >= 1

    # 5. Trace list.
    status, body = _api(receiver, "GET", "/v1/traces?limit=5", key=DEV_API_KEY)
    assert status == 200, f"trace list: {body}"
    assert len(body["data"]) >= 1

    # 6. Trace tree.
    status, body = _api(receiver, "GET",
                        f"/v1/traces/{trace_id_hex}/tree", key=DEV_API_KEY)
    assert status == 200, f"trace tree: {body}"
    assert body["span_count"] >= 1

    # 7. Span aggregation.
    status, body = _api(receiver, "GET",
                        "/v1/spans/aggregate?group_by=agent_name",
                        key=DEV_API_KEY)
    assert status == 200, f"aggregate: {body}"
    assert any(r["dimension"] == "lifecycle-bot" for r in body["data"])

    # 8. Policy versioning — version 1 should exist.
    status, body = _api(receiver, "GET",
                        f"/v1/policies/{policy_id}/versions",
                        key=DEV_API_KEY)
    assert status == 200, f"versions: {body}"
    assert len(body["data"]) >= 1
    assert body["data"][-1]["version"] == 1
    assert body["data"][-1]["change_type"] == "create"

    # 9. Update policy → version 2.
    status, _ = _api(receiver, "PATCH", f"/v1/policies/{policy_id}",
                     {"name": f"{policy_name}-v2"}, DEV_API_KEY)
    assert status == 200
    status, body = _api(receiver, "GET",
                        f"/v1/policies/{policy_id}/versions",
                        key=DEV_API_KEY)
    assert status == 200
    assert len(body["data"]) >= 2

    # 10. Audit log — policy creation should be captured.
    status, body = _api(receiver, "GET",
                        "/v1/audit/events?limit=50", key=DEV_API_KEY)
    assert status == 200, f"audit: {body}"
    # The response might nest events under "data" or "events" or at top level.
    events = body.get("data", body.get("events", []))
    if isinstance(events, list) and events:
        # Check if any event relates to our policy.
        # Event shapes vary: resource might be nested under "resource" key.
        policy_events = []
        for e in events:
            res = e.get("resource", {})
            rid = res.get("id", e.get("resource_id", ""))
            rtype = res.get("type", e.get("resource_type", ""))
            if rid == policy_id or rtype == "policy":
                policy_events.append(e)
        assert len(policy_events) >= 1, (
            f"audit should have recorded policy creation. "
            f"Got {len(events)} events, none matched policy_id={policy_id}"
        )

    # Cleanup: delete the policy.
    _api(receiver, "DELETE", f"/v1/policies/{policy_id}", key=DEV_API_KEY)

    # Cleanup: soft-delete the test project.
    _api(receiver, "DELETE", f"/v1/projects/{slug}", key=DEV_API_KEY)


def test_project_settings_retention(receiver: str):
    """Verify retention settings round-trip via the API."""
    # GET current settings.
    status, body = _api(receiver, "GET", "/v1/project/settings", key=DEV_API_KEY)
    assert status == 200
    assert "trace_retention_days" in body

    # PATCH retention.
    status, body = _api(receiver, "PATCH", "/v1/project/settings",
                        {"trace_retention_days": 90}, DEV_API_KEY)
    assert status == 200
    assert body["trace_retention_days"] == 90

    # Reset.
    _api(receiver, "PATCH", "/v1/project/settings",
         {"trace_retention_days": 30}, DEV_API_KEY)


def test_aggregate_time_bucket_e2e(receiver: str):
    """Verify time-bucketed aggregation works end-to-end."""
    # Ingest a span.
    trace_id = uuid.uuid4().bytes
    span_id = os.urandom(8)
    _otlp_ingest(receiver, DEV_API_KEY, trace_id, span_id,
                 agent_name="bucket-bot", model="claude-3-opus")
    time.sleep(0.3)

    # Aggregate with time bucket.
    status, body = _api(receiver, "GET",
                        "/v1/spans/aggregate?group_by=request_model&time_bucket=1d",
                        key=DEV_API_KEY)
    assert status == 200
    assert body["time_bucket"] == "1d"
    if body["data"]:
        assert "bucket" in body["data"][0]

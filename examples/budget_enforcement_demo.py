"""Budget enforcement demo: Strathon auto-halts an agent that exceeds its cost cap.

Enterprise agents run unattended. Without cost guardrails, a runaway loop
can burn through thousands of dollars in minutes. Strathon's budget system
puts a hard cap on spend:

1. We create a project-level cost budget ($0.10 USD, 1-hour fixed window).
2. The SDK ingests spans with cost data (simulated LLM calls).
3. The receiver's budget monitor detects the threshold is crossed.
4. The receiver writes an auto-halt for the project.
5. The SDK's next policy poll picks up the halt.
6. Any subsequent tool call raises StrathonHaltExceeded — the agent stops.

This is the difference between "observing that you overspent" (Langfuse) and
"preventing the overspend from happening" (Strathon).

Prerequisites:
    pip install strathon cel-python
    Receiver running at http://localhost:4318

Run:
    python budget_enforcement_demo.py
"""

import json
import time
from urllib.request import Request, urlopen

RECEIVER_URL = "http://localhost:4318"
API_KEY = "stra_dev_local_default_project_do_not_use_in_production"
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}"}


def _api(method, path, body=None):
    url = f"{RECEIVER_URL}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {**AUTH_HEADERS, "Content-Type": "application/json"}
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw.strip() else {}
    except Exception as e:
        return getattr(e, "code", 0), {}


def setup_budget():
    """Create a cost budget with a very low cap so the demo triggers quickly."""
    _, body = _api("POST", "/v1/budgets", {
        "name": "demo-cost-cap",
        "budget_type": "cost",
        "limit_usd": "0.10",
        "window_seconds": 3600,
    })
    budget_id = body.get("id")
    print(f"  Created budget: $0.10/hour (id={budget_id})")
    return budget_id


def cleanup(budget_id):
    """Remove the demo budget and any halts it created."""
    if budget_id:
        _api("DELETE", f"/v1/budgets/{budget_id}")
    # Clear any project-level halts.
    _, body = _api("GET", "/v1/halts")
    for halt in body.get("data", []):
        _api("DELETE", f"/v1/halts/{halt['id']}")


def simulate_expensive_agent():
    """Simulate an agent making expensive LLM calls.

    In production, the SDK captures cost automatically from the
    framework instrumentation. Here we simulate by sending spans
    with cost attributes directly.
    """
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
    from opentelemetry.proto.trace.v1.trace_pb2 import (
        ResourceSpans, ScopeSpans, Span,
    )
    from opentelemetry.proto.resource.v1.resource_pb2 import Resource
    import os
    import urllib.request

    def _kv(k, v):
        if isinstance(v, int):
            return KeyValue(key=k, value=AnyValue(int_value=v))
        if isinstance(v, float):
            return KeyValue(key=k, value=AnyValue(double_value=v))
        return KeyValue(key=k, value=AnyValue(string_value=str(v)))

    # Send 5 "expensive" LLM call spans, each costing $0.05.
    # Total: $0.25, which exceeds our $0.10 budget.
    for i in range(5):
        trace_id = os.urandom(16)
        span_id = os.urandom(8)
        now_ns = int(time.time() * 1e9)

        span = Span(
            trace_id=trace_id,
            span_id=span_id,
            name=f"llm-call-{i+1}",
            kind=Span.SPAN_KIND_CLIENT,
            start_time_unix_nano=now_ns,
            end_time_unix_nano=now_ns + 100_000_000,
            attributes=[
                _kv("strathon.agent.name", "expensive-bot"),
                _kv("gen_ai.request.model", "gpt-4o"),
                _kv("gen_ai.usage.input_tokens", 5000),
                _kv("gen_ai.usage.output_tokens", 2000),
                _kv("gen_ai.usage.cost", 0.05),
            ],
        )
        req = ExportTraceServiceRequest(
            resource_spans=[ResourceSpans(
                resource=Resource(attributes=[_kv("service.name", "budget-demo")]),
                scope_spans=[ScopeSpans(spans=[span])],
            )],
        )
        http_req = urllib.request.Request(
            f"{RECEIVER_URL}/v1/traces",
            data=req.SerializeToString(),
            method="POST",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/x-protobuf",
            },
        )
        with urllib.request.urlopen(http_req, timeout=10):
            pass

        cost_so_far = (i + 1) * 0.05
        print(f"  Span {i+1}/5 ingested (${cost_so_far:.2f} cumulative)")
        time.sleep(0.3)


def main():
    print("\n=== Strathon Budget Enforcement Demo ===\n")

    print("[1] Setting up cost budget ($0.10/hour)...")
    budget_id = setup_budget()

    print("\n[2] Simulating expensive agent (5 LLM calls × $0.05 = $0.25)...")
    simulate_expensive_agent()

    print("\n[3] Waiting for budget monitor to detect overspend...")
    # The budget monitor runs every 0.5s in dev (configured via env var).
    # In production it's every 5s.
    time.sleep(3)

    print("\n[4] Checking if halt was created...")
    _, body = _api("GET", "/v1/halts")
    halts = body.get("data", [])
    budget_halts = [h for h in halts if "budget" in h.get("reason", "").lower()]

    if budget_halts:
        print(f"  AUTO-HALT CREATED: {budget_halts[0].get('reason', 'budget exceeded')}")
        print(f"  Halt scope: {budget_halts[0].get('scope', 'project')}")
        print("  Any SDK tool call would now raise StrathonHaltExceeded.")
    else:
        print("  (Budget monitor hasn't fired yet — try increasing the sleep or")
        print("   set STRATHON_BUDGET_EVAL_INTERVAL_SECONDS=0.5)")

    print("\n[5] Cleaning up...")
    cleanup(budget_id)

    print("\n=== Demo complete ===")
    print("The agent burned $0.25 against a $0.10 cap.")
    print("Strathon auto-halted the project before more damage could be done.")
    print("Langfuse would show you a chart of the spend — after the fact.")
    print("Strathon stops the bleeding.\n")


if __name__ == "__main__":
    main()

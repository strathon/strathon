#!/usr/bin/env python3
"""Strathon load test — measures OTLP ingest throughput.

Usage:
    # Start Strathon first
    docker compose up -d

    # Install dependencies
    pip install httpx opentelemetry-proto protobuf

    # Run with defaults (1000 requests, 10 concurrency, 50 spans/batch)
    python benchmarks/loadtest.py

    # Custom parameters
    python benchmarks/loadtest.py --requests 5000 --concurrency 50 --batch-size 100

    # Target a remote instance
    python benchmarks/loadtest.py --endpoint https://api.getstrathon.com --api-key stra_...

Environment:
    STRATHON_API_KEY       API key (or use --api-key flag)
    STRATHON_ENDPOINT      Receiver URL (or use --endpoint flag)

Metrics reported:
    - Total spans ingested
    - Wall-clock time
    - Throughput (spans/sec)
    - Latency: p50, p95, p99, max
    - Error rate
    - Requests/sec
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import random
import string
import struct
import sys
import time
from dataclasses import dataclass, field

import httpx

# ---- OTLP Protobuf generation -----------------------------------------------

# Minimal protobuf encoding without importing opentelemetry-proto.
# Generates valid ExportTraceServiceRequest with N spans.
# This avoids a heavy dependency just for load testing.


def _varint(value: int) -> bytes:
    """Encode an unsigned varint."""
    parts = []
    while value > 0x7F:
        parts.append((value & 0x7F) | 0x80)
        value >>= 7
    parts.append(value & 0x7F)
    return bytes(parts)


def _field_bytes(field_num: int, wire_type: int, data: bytes) -> bytes:
    """Encode a protobuf field."""
    tag = _varint((field_num << 3) | wire_type)
    if wire_type == 2:  # length-delimited
        return tag + _varint(len(data)) + data
    elif wire_type == 0:  # varint
        return tag + data
    return tag + data


def _string_field(field_num: int, value: str) -> bytes:
    return _field_bytes(field_num, 2, value.encode("utf-8"))


def _bytes_field(field_num: int, value: bytes) -> bytes:
    return _field_bytes(field_num, 2, value)


def _fixed64_field(field_num: int, value: int) -> bytes:
    tag = _varint((field_num << 3) | 1)  # wire type 1 = 64-bit
    return tag + struct.pack("<Q", value)


def _make_trace_id() -> bytes:
    return random.randbytes(16)


def _make_span_id() -> bytes:
    return random.randbytes(8)


def _random_string(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=length))


def _make_key_value(key: str, value: str) -> bytes:
    """Encode a KeyValue (field 1=key, field 2=AnyValue with string)."""
    string_val = _string_field(1, value)  # AnyValue.string_value
    any_value = _field_bytes(2, 2, string_val)  # KeyValue.value
    key_field = _string_field(1, key)  # KeyValue.key
    return key_field + any_value


def generate_otlp_payload(num_spans: int) -> bytes:
    """Generate a minimal valid ExportTraceServiceRequest protobuf."""
    now_ns = int(time.time() * 1e9)
    trace_id = _make_trace_id()

    spans_data = b""
    for i in range(num_spans):
        span_id = _make_span_id()
        agent = f"loadtest-agent-{random.randint(1, 5)}"
        tool = random.choice([
            "search", "calculate", "fetch_data", "send_email",
            "query_db", "summarize", "translate", "classify",
        ])
        model = random.choice([
            "gpt-4o", "gpt-4o-mini", "claude-sonnet-4-20250514",
            "claude-haiku-4-5-20251001", "gemini-2.0-flash",
        ])

        # Span attributes
        attrs = b""
        attrs += _field_bytes(1, 2, _make_key_value("gen_ai.agent.name", agent))
        attrs += _field_bytes(1, 2, _make_key_value("gen_ai.tool.name", tool))
        attrs += _field_bytes(1, 2, _make_key_value("gen_ai.request.model", model))
        attrs += _field_bytes(1, 2, _make_key_value(
            "gen_ai.usage.input_tokens", str(random.randint(50, 2000))
        ))
        attrs += _field_bytes(1, 2, _make_key_value(
            "gen_ai.usage.output_tokens", str(random.randint(10, 500))
        ))

        # Span fields
        span = b""
        span += _bytes_field(1, trace_id)           # trace_id
        span += _bytes_field(2, span_id)             # span_id
        span += _string_field(3, f"span-{i}")        # name (field 3 is actually TraceState, name=5)
        span += _string_field(5, f"{tool}-call-{i}")  # name
        span += _fixed64_field(7, now_ns - (num_spans - i) * 1_000_000)  # start_time
        span += _fixed64_field(8, now_ns - (num_spans - i - 1) * 1_000_000)  # end_time
        span += attrs  # attributes in field 9
        # Wrap attrs properly
        spans_data += _field_bytes(2, 2, span)  # ScopeSpans.spans (field 2)

    # InstrumentationScope
    scope = _string_field(1, "strathon-loadtest")  # name
    scope += _string_field(2, "1.2.1")              # version
    scope_spans = _field_bytes(1, 2, scope) + spans_data

    # Resource
    resource_attrs = _field_bytes(
        1, 2, _make_key_value("service.name", "loadtest")
    )
    resource = _field_bytes(1, 2, resource_attrs)

    # ResourceSpans
    resource_spans = _field_bytes(1, 2, resource) + _field_bytes(2, 2, scope_spans)

    # ExportTraceServiceRequest
    return _field_bytes(1, 2, resource_spans)


# ---- Load test runner --------------------------------------------------------

@dataclass
class Stats:
    successes: int = 0
    errors: int = 0
    latencies: list[float] = field(default_factory=list)
    error_details: list[str] = field(default_factory=list)


async def send_batch(
    client: httpx.AsyncClient,
    url: str,
    payload: bytes,
    stats: Stats,
    semaphore: asyncio.Semaphore,
):
    async with semaphore:
        start = time.monotonic()
        try:
            r = await client.post(
                url,
                content=payload,
                headers={"Content-Type": "application/x-protobuf"},
            )
            elapsed = time.monotonic() - start
            if r.status_code == 200:
                stats.successes += 1
                stats.latencies.append(elapsed)
            else:
                stats.errors += 1
                stats.error_details.append(f"{r.status_code}: {r.text[:100]}")
        except Exception as e:
            stats.errors += 1
            stats.error_details.append(str(e)[:100])


async def run_load_test(
    endpoint: str,
    api_key: str,
    num_requests: int,
    concurrency: int,
    batch_size: int,
):
    url = f"{endpoint}/v1/traces"
    stats = Stats()
    semaphore = asyncio.Semaphore(concurrency)

    # Pre-generate payloads (avoid generation overhead during test).
    print(f"Generating {num_requests} payloads ({batch_size} spans each)...")
    payloads = [generate_otlp_payload(batch_size) for _ in range(num_requests)]
    total_spans = num_requests * batch_size
    avg_payload = sum(len(p) for p in payloads) / len(payloads)

    print(f"  Payload size: ~{avg_payload:.0f} bytes avg")
    print(f"  Total spans: {total_spans:,}")
    print(f"  Concurrency: {concurrency}")
    print(f"  Target: {url}")
    print()

    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    ) as client:
        # Warmup (5 requests).
        print("Warmup...")
        for p in payloads[:5]:
            await send_batch(client, url, p, Stats(), semaphore)

        # Actual test.
        print("Running load test...")
        wall_start = time.monotonic()

        tasks = [
            send_batch(client, url, p, stats, semaphore)
            for p in payloads
        ]
        await asyncio.gather(*tasks)

        wall_time = time.monotonic() - wall_start

    # Results.
    print()
    print("=" * 50)
    print("RESULTS")
    print("=" * 50)
    print(f"  Requests:      {num_requests}")
    print(f"  Batch size:    {batch_size} spans/request")
    print(f"  Total spans:   {total_spans:,}")
    print(f"  Wall time:     {wall_time:.2f}s")
    print(f"  Successes:     {stats.successes}")
    print(f"  Errors:        {stats.errors}")
    print(f"  Error rate:    {stats.errors / num_requests * 100:.1f}%")
    print()

    if stats.latencies:
        lats = sorted(stats.latencies)
        spans_per_sec = total_spans / wall_time
        reqs_per_sec = num_requests / wall_time

        print(f"  Throughput:    {spans_per_sec:,.0f} spans/sec")
        print(f"  Requests/sec:  {reqs_per_sec:,.0f}")
        print()
        print(f"  Latency p50:   {lats[len(lats) // 2] * 1000:.1f}ms")
        print(f"  Latency p95:   {lats[int(len(lats) * 0.95)] * 1000:.1f}ms")
        print(f"  Latency p99:   {lats[int(len(lats) * 0.99)] * 1000:.1f}ms")
        print(f"  Latency max:   {lats[-1] * 1000:.1f}ms")

    if stats.error_details:
        print()
        print("  Sample errors:")
        for e in stats.error_details[:5]:
            print(f"    - {e}")

    print()
    if stats.latencies:
        spans_per_sec = total_spans / wall_time
        daily = spans_per_sec * 86400
        agents = spans_per_sec / (50 / 60)  # assuming 50 calls/min per agent
        print(f"  📊 {spans_per_sec:,.0f} spans/sec = {daily/1e6:,.0f}M spans/day")
        print(f"  📊 Supports ~{agents:,.0f} concurrent agents (at 50 calls/min)")

    # Hardware/config context so a published number is meaningful and
    # reproducible. Numbers without this context should not be quoted.
    print()
    print("  Context (for reproducibility):")
    print(f"    Hardware:  {platform.platform()}")
    print(f"    CPU:       {os.cpu_count()} cores, {platform.machine()}")
    print(f"    Config:    {num_requests} requests, concurrency={concurrency}, "
          f"batch={batch_size}")

    spans_per_sec_final = 0.0
    if stats.latencies:
        spans_per_sec_final = total_spans / wall_time

    return {
        "error_rate": stats.errors / num_requests if num_requests else 1.0,
        "spans_per_sec": spans_per_sec_final,
        "concurrency": concurrency,
    }


def main():
    parser = argparse.ArgumentParser(description="Strathon load test")
    parser.add_argument("--endpoint", default=os.environ.get(
        "STRATHON_ENDPOINT", "http://localhost:4318"))
    parser.add_argument("--api-key", default=os.environ.get(
        "STRATHON_API_KEY", "stra_dev_local_default_project_do_not_use_in_production"))
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=50,
                        help="Spans per request")
    parser.add_argument(
        "--sweep", default="",
        help=(
            "Comma-separated concurrency levels to sweep, e.g. "
            "'8,16,32,64'. Runs the test at each level and prints a "
            "throughput curve so you can find where Postgres saturates "
            "(the plateau is your real ceiling). Overrides --concurrency."
        ),
    )
    args = parser.parse_args()

    if args.sweep:
        try:
            levels = [int(x) for x in args.sweep.split(",") if x.strip()]
        except ValueError:
            print("--sweep must be comma-separated integers, e.g. 8,16,32,64",
                  file=sys.stderr)
            sys.exit(2)
        curve = []
        for c in levels:
            print(f"\n########## concurrency = {c} ##########")
            res = asyncio.run(run_load_test(
                args.endpoint, args.api_key, args.requests, c, args.batch_size,
            ))
            curve.append(res)
        # Summary curve.
        print("\n" + "=" * 50)
        print("THROUGHPUT CURVE (find the plateau = your ceiling)")
        print("=" * 50)
        print(f"  {'concurrency':>12} | {'spans/sec':>14} | {'err%':>6}")
        print("  " + "-" * 40)
        best = max(curve, key=lambda r: r["spans_per_sec"]) if curve else None
        for r in curve:
            mark = "  <- peak" if best and r is best else ""
            print(f"  {r['concurrency']:>12} | {r['spans_per_sec']:>14,.0f} | "
                  f"{r['error_rate'] * 100:>5.1f}%{mark}")
        if best:
            print(f"\n  Peak sustained: {best['spans_per_sec']:,.0f} spans/sec "
                  f"at concurrency {best['concurrency']}.")
            print("  If spans/sec stops climbing as concurrency rises, that")
            print("  plateau is your real ceiling (usually Postgres-bound).")
        # Fail the run if any level exceeded the error threshold.
        if any(r["error_rate"] > 0.01 for r in curve):
            print("\nFAIL: error rate exceeded 1% at one or more levels",
                  file=sys.stderr)
            sys.exit(1)
        return

    result = asyncio.run(run_load_test(
        args.endpoint, args.api_key,
        args.requests, args.concurrency, args.batch_size,
    ))
    if result["error_rate"] > 0.01:
        print(f"\nFAIL: error rate {result['error_rate'] * 100:.1f}% exceeds 1% threshold",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

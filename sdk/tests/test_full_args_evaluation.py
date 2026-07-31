"""Policy evaluation must see the full tool arguments, not a truncated copy.

Tool arguments used to be truncated (to ~1500 chars) in the same attribute dict
that feeds policy evaluation, so an attacker could pad an argument to push the
malicious substring past the limit and out of a content policy's view -- the
SQL-injection, exfiltration, and prompt-injection templates all match on
strathon.tool.args.contains(...). Truncation now happens only in the OTel span
layer (SpanLimits), so the value a policy sees stays full while the stored span
is still bounded.

These tests assert both halves: the builder keeps the full argument, and the
OTel SpanLimits truncation shortens only the recorded span attribute.
"""

from __future__ import annotations

from strathon.policy.steer import build_tool_span_attrs


class _Tool:
    name = "send_email"


def test_builder_keeps_full_args_for_evaluation():
    """The attribute dict handed to check_policy contains the whole payload,
    including content that would fall past the old 1500-char limit."""
    marker = "DROP TABLE users"
    payload = {"body": "x" * 3000 + marker}
    attrs = build_tool_span_attrs(_Tool(), payload, "langgraph")
    args = attrs["strathon.tool.args"]
    assert len(args) > 1500
    assert marker in args, "malicious content past 1500 chars was lost before eval"


def test_otel_spanlimits_truncates_only_the_span_copy():
    """With SpanLimits configured, the recorded span attribute is truncated for
    storage while the source value the enforcer read stays full."""
    from opentelemetry.sdk.trace import SpanLimits, TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider(span_limits=SpanLimits(max_span_attribute_length=1500))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    full_value = "y" * 2000 + "DROP TABLE users"
    with tracer.start_as_current_span("tool.call") as span:
        span.set_attribute("strathon.tool.args", full_value)

    recorded = exporter.get_finished_spans()[0].attributes["strathon.tool.args"]
    assert len(recorded) <= 1500
    assert len(recorded) < len(full_value)


def test_client_config_sets_a_span_attribute_bound_by_default():
    """The client's default config carries a positive span attribute bound, so
    a fresh Client truncates pathological span values without extra setup."""
    from strathon.config import Config

    assert Config().max_span_attr_len > 0

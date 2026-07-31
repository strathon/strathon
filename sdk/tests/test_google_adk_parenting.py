"""Span-parenting regression tests for the google_adk adapter.

ADK invokes the plugin's tool and model callbacks with a context that reaches the
current ``invocation_id``. Without an invocation-level span, every tool and model
span starts its own root trace, so one agent invocation shows as several agents.
The adapter now opens an invocation span in ``before_run_callback`` and parents the
tool and model spans under it.

These build the real plugin class against a minimal ``BasePlugin`` stub and drive
the callbacks with fake contexts. They mock only ``google.adk.plugins.base_plugin``
so they do not disturb ``google.protobuf`` (used by the OTLP exporter), and they
run in a subprocess-free way by installing the stub before importing the adapter
module lazily inside the test.
"""

import asyncio
import sys
import types

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


def _install_adk_stub():
    """Register a minimal google.adk.plugins.base_plugin without clobbering
    the real google namespace package (google.protobuf must keep working)."""
    if "google.adk.plugins.base_plugin" in sys.modules:
        return
    # Reuse the real google package if present; only add the adk subpath.
    google_mod = sys.modules.get("google") or types.ModuleType("google")
    google_mod.__path__ = getattr(google_mod, "__path__", [])
    adk_mod = types.ModuleType("google.adk")
    adk_mod.__path__ = []
    plugins_mod = types.ModuleType("google.adk.plugins")
    plugins_mod.__path__ = []
    base_plugin_mod = types.ModuleType("google.adk.plugins.base_plugin")

    class BasePlugin:
        def __init__(self, name=None):
            self.name = name

    base_plugin_mod.BasePlugin = BasePlugin
    sys.modules.setdefault("google", google_mod)
    sys.modules["google.adk"] = adk_mod
    sys.modules["google.adk.plugins"] = plugins_mod
    sys.modules["google.adk.plugins.base_plugin"] = base_plugin_mod


class _FakeClient:
    def __init__(self, tracer):
        self.tracer = tracer
        self.policy_enforcer = None


class _InvCtx:
    invocation_id = "inv-123"

    class agent:
        name = "researcher"


class _ToolCtx:
    invocation_id = "inv-123"
    agent_name = "researcher"


class _Tool:
    name = "search"


@pytest.fixture
def tracer_and_exporter():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def test_tool_span_nests_under_invocation(tracer_and_exporter):
    _install_adk_stub()
    from strathon.instrumentation.google_adk import _build_plugin_class

    tracer, exporter = tracer_and_exporter
    Plugin = _build_plugin_class()
    assert Plugin is not None
    plugin = Plugin(_FakeClient(tracer))

    async def run():
        await plugin.before_run_callback(invocation_context=_InvCtx())
        await plugin.after_tool_callback(
            tool=_Tool(), tool_args={"q": "x"}, tool_context=_ToolCtx(), result={"ok": 1}
        )
        await plugin.after_run_callback(invocation_context=_InvCtx())

    asyncio.run(run())

    spans = {s.name: s for s in exporter.get_finished_spans()}
    invocation = next(s for n, s in spans.items() if "invocation" in n)
    tool = next(s for n, s in spans.items() if ".tool." in n)
    assert invocation.context.trace_id == tool.context.trace_id
    assert tool.parent is not None
    assert tool.parent.span_id == invocation.context.span_id


def test_tool_span_is_root_without_invocation_but_does_not_error(tracer_and_exporter):
    # Defensive: if before_run_callback never fired, the tool span falls back to
    # a root rather than crashing inside instrumentation.
    _install_adk_stub()
    from strathon.instrumentation.google_adk import _build_plugin_class

    tracer, exporter = tracer_and_exporter
    plugin = _build_plugin_class()(_FakeClient(tracer))

    async def run():
        await plugin.after_tool_callback(
            tool=_Tool(), tool_args={"q": "x"}, tool_context=_ToolCtx(), result={"ok": 1}
        )

    asyncio.run(run())
    spans = {s.name: s for s in exporter.get_finished_spans()}
    tool = next(s for n, s in spans.items() if ".tool." in n)
    assert tool.parent is None  # root, no crash

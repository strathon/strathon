"""Span-parenting regression tests for the pydantic_ai adapter.

Every capability hook receives a RunContext carrying a stable ``run_id``. Without
a run-level span, each tool and model span starts its own root trace, so one agent
run shows as several agents. The adapter now opens a run span in ``before_run``
keyed by ``run_id`` and parents tool and model spans under it. ``for_run`` defaults
to returning ``self``, so one capability instance serves every run; keying by
``run_id`` keeps concurrent runs from cross-parenting.

Built against a minimal AbstractCapability stub so no pydantic-ai install is
needed. The stub only needs the ``before_tool_execute`` attribute the adapter
feature-checks for.
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


def _install_pydantic_ai_stub():
    if "pydantic_ai.capabilities" in sys.modules:
        return
    pai = types.ModuleType("pydantic_ai")
    pai.__path__ = []
    caps = types.ModuleType("pydantic_ai.capabilities")
    exc = types.ModuleType("pydantic_ai.exceptions")

    class AbstractCapability:
        # The adapter feature-checks for this attribute before subclassing.
        def before_tool_execute(self, ctx, *, call, tool_def, args):
            return args

    class CapabilityOrdering:
        def __init__(self, position=None):
            self.position = position

    class SkipToolExecution(Exception):
        def __init__(self, *a, **k):
            super().__init__()

    caps.AbstractCapability = AbstractCapability
    caps.CapabilityOrdering = CapabilityOrdering
    exc.SkipToolExecution = SkipToolExecution
    sys.modules["pydantic_ai"] = pai
    sys.modules["pydantic_ai.capabilities"] = caps
    sys.modules["pydantic_ai.exceptions"] = exc


class _FakeClient:
    def __init__(self, tracer):
        self.tracer = tracer
        self.policy_enforcer = None


class _Ctx:
    run_id = "run-1"

    class agent:
        name = "researcher"


class _Call:
    tool_name = "search"


class _ToolDef:
    name = "search"


@pytest.fixture
def tracer_and_exporter():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def _firewall(tracer):
    _install_pydantic_ai_stub()
    from strathon.instrumentation.pydantic_ai import _build_firewall_class

    Firewall = _build_firewall_class()
    assert Firewall is not None
    return Firewall(client=_FakeClient(tracer))


def test_tool_span_nests_under_run(tracer_and_exporter):
    tracer, exporter = tracer_and_exporter
    fw = _firewall(tracer)

    async def run():
        await fw.before_run(_Ctx())
        async def handler(a):
            return "result"
        await fw.wrap_tool_execute(
            _Ctx(), call=_Call(), tool_def=_ToolDef(), args={"q": "x"}, handler=handler
        )
        await fw.after_run(_Ctx())

    asyncio.run(run())
    spans = {s.name: s for s in exporter.get_finished_spans()}
    run_span = next(s for n, s in spans.items() if ".run." in n)
    tool = next(s for n, s in spans.items() if ".tool." in n)
    assert run_span.context.trace_id == tool.context.trace_id
    assert tool.parent is not None
    assert tool.parent.span_id == run_span.context.span_id


def test_two_runs_do_not_cross_parent(tracer_and_exporter):
    tracer, exporter = tracer_and_exporter
    fw = _firewall(tracer)

    class Ctx2:
        run_id = "run-2"

        class agent:
            name = "other"

    async def one_run(ctx):
        await fw.before_run(ctx)
        async def handler(a):
            return "r"
        await fw.wrap_tool_execute(
            ctx, call=_Call(), tool_def=_ToolDef(), args={"q": "x"}, handler=handler
        )
        await fw.after_run(ctx)

    async def run():
        await asyncio.gather(one_run(_Ctx()), one_run(Ctx2()))

    asyncio.run(run())
    spans = list(exporter.get_finished_spans())
    runs = {s.attributes.get("strathon.agent.name"): s for s in spans if ".run." in s.name}
    tools = [s for s in spans if ".tool." in s.name]
    # each tool parents under a run span, and the two runs are distinct traces-by-parent
    run_span_ids = {s.context.span_id for s in runs.values()}
    for t in tools:
        assert t.parent is not None
        assert t.parent.span_id in run_span_ids

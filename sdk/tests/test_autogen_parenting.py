"""Span-parenting regression tests for the autogen adapter.

AutoGen runs tool calls in a separate asyncio task. A task copies the current
OpenTelemetry context when it is created, so the agent span has to be attached to
the context before the tool task is spawned, or the tool span starts its own root
trace and the dashboard shows one agent run as several agents.

These drive the real adapter wrappers against a tool span emitted from a child
task (the exact condition that broke) and assert the tool span nests under the
agent span. They need no autogen install -- they exercise the wrapper functions
directly with an in-memory tracer.
"""

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from strathon.instrumentation.autogen import _wrap_on_messages, _wrap_team_run


@pytest.fixture
def tracer_and_exporter():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


class _Resp:
    chat_message = None


def _spans_by_name(exporter):
    return {s.name: s for s in exporter.get_finished_spans()}


def test_tool_span_nests_under_agent_across_task_boundary(tracer_and_exporter):
    tracer, exporter = tracer_and_exporter

    async def tool():
        tracer.start_span("autogen.tool.search").end()

    async def on_messages(self, messages, cancellation_token=None):
        # Tool runs in its own task, as autogen does.
        await asyncio.create_task(tool())
        return _Resp()

    class Agent:
        name = "researcher"

    wrapped = _wrap_on_messages(on_messages, tracer)
    asyncio.run(wrapped(Agent(), ["hi"]))

    spans = _spans_by_name(exporter)
    agent = spans["autogen.agent.researcher"]
    tool_span = spans["autogen.tool.search"]
    assert agent.context.trace_id == tool_span.context.trace_id
    assert tool_span.parent is not None
    assert tool_span.parent.span_id == agent.context.span_id


def test_concurrent_agents_in_team_do_not_cross_parent(tracer_and_exporter):
    tracer, exporter = tracer_and_exporter

    async def tool(n):
        span = tracer.start_span(f"autogen.tool.{n}")
        await asyncio.sleep(0.01)
        span.end()

    async def on_messages(self, messages, cancellation_token=None):
        await asyncio.create_task(tool(self.name))
        return _Resp()

    class Agent:
        def __init__(self, name):
            self.name = name

    wrap_agent = _wrap_on_messages(on_messages, tracer)

    async def team_run(self, *args, **kwargs):
        await asyncio.gather(
            wrap_agent(Agent("a1"), ["hi"]),
            wrap_agent(Agent("a2"), ["hi"]),
        )
        return type("R", (), {"messages": None, "stop_reason": None})()

    class Team:
        _team_id = "team-1"

    wrapped_team = _wrap_team_run(team_run, tracer)
    asyncio.run(wrapped_team(Team(), "task"))

    spans = _spans_by_name(exporter)
    a1 = spans["autogen.agent.a1"]
    a2 = spans["autogen.agent.a2"]
    team = spans["autogen.team.team-1"]
    # Each tool nests under its own agent, not the other's.
    assert spans["autogen.tool.a1"].parent.span_id == a1.context.span_id
    assert spans["autogen.tool.a2"].parent.span_id == a2.context.span_id
    # Both agents nest under the team, one trace overall.
    assert a1.parent.span_id == team.context.span_id
    assert a2.parent.span_id == team.context.span_id
    assert len({s.context.trace_id for s in spans.values()}) == 1

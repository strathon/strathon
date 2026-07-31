"""Regression tests for the claude_agent adapter.

Covers the two wrap fixes and the session parenting:

- Module ``query()`` is an async generator. The wrapper must iterate and re-yield
  (not await it) and record the session id from the stream.
- ``ClaudeSDKClient.query()`` returns None and takes session_id as an argument.
  The wrapper opens a session span keyed by that session_id.
- The PostToolUse hook parents its tool span under the session span (bridged by
  session_id), and the Stop hook closes the session span.

These drive the real wrapper/hook functions with fakes; no claude-agent-sdk
install is needed. The tool and stop hooks are built via the ImportError branch of
create_strathon_hooks (no HookMatcher), which returns plain dicts.
"""

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

import strathon.instrumentation.claude_agent as ca


def _hook_fn(entry):
    """Extract the hook callable from a create_strathon_hooks entry.

    The entry is a HookMatcher (when claude_agent_sdk is installed) or a plain
    dict (the ImportError fallback). Handle both so the test does not depend on
    whether the SDK is present in the environment.
    """
    hooks = entry["hooks"] if isinstance(entry, dict) else entry.hooks
    return hooks[0]


class _FakeClient:
    def __init__(self, tracer):
        self.tracer = tracer
        self.policy_enforcer = None


class _Msg:
    def __init__(self, session_id=None, text=None):
        if session_id is not None:
            self.session_id = session_id
        self._text = text


@pytest.fixture(autouse=True)
def _reset_session_tree():
    # The session tree is module-level; reset it between tests.
    ca._SESSION_TREE = None
    yield
    ca._SESSION_TREE = None


@pytest.fixture
def tracer_and_exporter():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def test_module_query_is_iterated_not_awaited(tracer_and_exporter):
    tracer, exporter = tracer_and_exporter

    async def fake_query(*args, **kwargs):
        # An async generator, like the real module query().
        yield _Msg(session_id="s-1")
        yield _Msg(text="hello")

    wrapped = ca._wrap_query(fake_query, tracer)

    async def run():
        return [msg async for msg in wrapped("prompt")]

    messages = asyncio.run(run())
    # All messages flow through (the wrapper is a transparent async generator).
    assert len(messages) == 2
    spans = {s.name: s for s in exporter.get_finished_spans()}
    query_span = spans["claude_agent.query"]
    assert query_span.attributes.get("gen_ai.conversation.id") == "s-1"


def test_client_query_opens_session_and_tool_nests_then_stop_closes(
    tracer_and_exporter,
):
    tracer, exporter = tracer_and_exporter
    client = _FakeClient(tracer)

    # 1. client.query(prompt, session_id=...) sends and returns None.
    async def fake_client_query(self, prompt, session_id="default"):
        return None

    wrapped_query = ca._wrap_client_query(fake_client_query, tracer)

    # 2. build the tool + stop hooks (ImportError branch -> plain dicts).
    hooks = ca.create_strathon_hooks(client)
    post_tool = _hook_fn(hooks["PostToolUse"][0])
    stop = _hook_fn(hooks["Stop"][0])

    class _SelfClient:
        name = "researcher"

    async def run():
        await wrapped_query(_SelfClient(), "do a search", session_id="sess-42")
        # PostToolUse fires with the session_id on its input.
        await post_tool(
            {"tool_name": "search", "tool_input": {"q": "x"}, "result": "ok",
             "session_id": "sess-42"},
            "tooluse-1",
            None,
        )
        # Stop fires, closing the session span.
        await stop({"session_id": "sess-42"}, None, None)

    asyncio.run(run())

    spans = {s.name: s for s in exporter.get_finished_spans()}
    session = next(s for n, s in spans.items() if n.startswith("claude_agent.client."))
    tool = next(s for n, s in spans.items() if ".tool." in n)
    assert session.context.trace_id == tool.context.trace_id
    assert tool.parent is not None
    assert tool.parent.span_id == session.context.span_id
    assert session.attributes.get("gen_ai.conversation.id") == "sess-42"


def test_tool_without_session_is_root_not_error(tracer_and_exporter):
    tracer, exporter = tracer_and_exporter
    client = _FakeClient(tracer)
    hooks = ca.create_strathon_hooks(client)
    post_tool = _hook_fn(hooks["PostToolUse"][0])

    async def run():
        # No session ever opened; tool span should be a root, not crash.
        await post_tool(
            {"tool_name": "search", "tool_input": {}, "result": "ok"},
            "tooluse-1",
            None,
        )

    asyncio.run(run())
    spans = {s.name: s for s in exporter.get_finished_spans()}
    tool = next(s for n, s in spans.items() if ".tool." in n)
    assert tool.parent is None

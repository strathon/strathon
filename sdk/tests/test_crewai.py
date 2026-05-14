"""Tests for CrewAI event-listener instrumentation.

The CrewAI event bus maintains its own ContextVar-based scope stack to
auto-correlate started/completed event pairs. Setting parent_event_id or
started_event_id manually causes the bus's pairing to mismatch. These
tests therefore emit events in proper nesting order WITHOUT manual IDs;
the bus fills them in correctly.
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from strathon import Client
from strathon.instrumentation.crewai import (
    StrathonCrewAIListener,
    _extract_usage,
    _provider_from_model,
    _truncate,
)


def make_client():
    return Client(
        api_key="test-key",
        endpoint="http://localhost:4318",
        set_global_tracer=False,
    )


def make_listener_with_bus():
    try:
        from crewai.events.event_bus import CrewAIEventsBus
    except ImportError:
        pytest.skip("crewai not installed")
    client = make_client()
    listener = StrathonCrewAIListener(client)
    bus = CrewAIEventsBus()
    listener.setup_listeners(bus)
    return listener, bus


def emit_sync(bus, event, source=None, timeout=5.0):
    """Emit an event and wait for handler thread to complete."""
    future = bus.emit(source or MagicMock(), event)
    if future is not None:
        future.result(timeout=timeout)


# ---- Pure helper tests ----


def test_provider_from_model_handles_litellm_prefix():
    assert _provider_from_model("anthropic/claude-opus-4-7") == "anthropic"
    assert _provider_from_model("openai/gpt-4o") == "openai"
    assert _provider_from_model("mistral/mistral-large") == "mistral"


def test_provider_from_model_falls_back_to_heuristics():
    assert _provider_from_model("gpt-4o") == "openai"
    assert _provider_from_model("claude-3-5-sonnet") == "anthropic"
    assert _provider_from_model("gemini-2.0-pro") == "google"


def test_provider_from_model_returns_none_for_unknown():
    assert _provider_from_model("custom-llm") is None
    assert _provider_from_model("") is None
    assert _provider_from_model(None) is None


def test_extract_usage_handles_litellm_style_dict():
    usage = {"prompt_tokens": 100, "completion_tokens": 250, "total_tokens": 350}
    assert _extract_usage(usage) == {
        "gen_ai.usage.input_tokens": 100,
        "gen_ai.usage.output_tokens": 250,
        "gen_ai.usage.total_tokens": 350,
    }


def test_extract_usage_handles_openai_style_dict():
    usage = {"input_tokens": 50, "output_tokens": 80}
    assert _extract_usage(usage) == {
        "gen_ai.usage.input_tokens": 50,
        "gen_ai.usage.output_tokens": 80,
    }


def test_extract_usage_handles_object():
    class Usage:
        prompt_tokens = 10
        completion_tokens = 20
        total_tokens = 30

    assert _extract_usage(Usage()) == {
        "gen_ai.usage.input_tokens": 10,
        "gen_ai.usage.output_tokens": 20,
        "gen_ai.usage.total_tokens": 30,
    }


def test_extract_usage_returns_empty_on_none():
    assert _extract_usage(None) == {}


def test_truncate_caps_long_strings():
    assert _truncate("hello", 100) == "hello"
    long_str = "x" * 5000
    truncated = _truncate(long_str, 100)
    assert "truncated" in truncated
    assert len(truncated) < 5000


# ---- Helpers ----


def _make_task(name="research_task", description="Research agent frameworks"):
    """Build a task-like MagicMock that survives CrewAI pydantic validation."""
    task = MagicMock()
    task.configure_mock(name=name)
    task.description = description
    task.expected_output = "A markdown summary"
    return task


def _make_crew_started_event(event_id="evt_crew_1"):
    from crewai.events import CrewKickoffStartedEvent

    crew = MagicMock()
    crew.agents = [MagicMock(role="researcher"), MagicMock(role="writer")]
    crew.tasks = [MagicMock(), MagicMock()]
    crew.process = "sequential"
    return CrewKickoffStartedEvent(
        event_id=event_id,
        crew_name="research_crew",
        crew=crew,
        inputs={"topic": "agent observability"},
    )


# ---- Lifecycle tests ----


def test_crew_kickoff_creates_root_span():
    listener, bus = make_listener_with_bus()
    emit_sync(bus, _make_crew_started_event(event_id="evt_crew_root"))
    assert "evt_crew_root" in listener._spans


def test_full_crew_lifecycle_starts_and_ends_root_span():
    from crewai.events import CrewKickoffCompletedEvent

    listener, bus = make_listener_with_bus()
    emit_sync(bus, _make_crew_started_event(event_id="evt_crew_full"))
    assert "evt_crew_full" in listener._spans

    # No started_event_id set; bus auto-pairs via its scope stack
    emit_sync(
        bus,
        CrewKickoffCompletedEvent(
            event_id="evt_crew_complete",
            crew_name="research_crew",
            output="final report",
            total_tokens=1500,
        ),
    )
    assert "evt_crew_full" not in listener._spans


def test_task_lifecycle_within_crew_scope():
    from crewai.events import (
        TaskStartedEvent,
        TaskCompletedEvent,
        CrewKickoffCompletedEvent,
    )
    from crewai.tasks.task_output import TaskOutput

    listener, bus = make_listener_with_bus()

    # Crew kickoff opens the scope
    emit_sync(bus, _make_crew_started_event(event_id="evt_crew"))

    task = _make_task(name="research_task")
    emit_sync(
        bus,
        TaskStartedEvent(
            event_id="evt_task_start",
            task=task,
            context="prior context",
        ),
    )
    assert "evt_task_start" in listener._spans

    emit_sync(
        bus,
        TaskCompletedEvent(
            event_id="evt_task_done",
            task=task,
            output=TaskOutput(
                description="Research task",
                raw="Found 5 frameworks",
                agent="researcher",
            ),
        ),
    )
    assert "evt_task_start" not in listener._spans

    # Close the crew scope too
    emit_sync(
        bus,
        CrewKickoffCompletedEvent(
            event_id="evt_crew_done",
            crew_name="research_crew",
            output="final",
            total_tokens=200,
        ),
    )
    assert "evt_crew" not in listener._spans


def test_agent_execution_lifecycle():
    """Uses real CrewAI Agent + BaseTool; AgentExecutionStartedEvent type-validates these strictly."""
    pytest.importorskip("openai")  # openai is part of crewai's base deps

    from crewai import Agent
    from crewai.tools import BaseTool
    from crewai.events import (
        AgentExecutionStartedEvent,
        AgentExecutionCompletedEvent,
    )

    class FakeWebSearchTool(BaseTool):
        name: str = "web_search"
        description: str = "search the web"

        def _run(self, query: str) -> str:
            return "results"

    agent = Agent(
        role="researcher",
        goal="Find accurate information",
        backstory="Senior research analyst",
        llm="gpt-4o-mini",  # identifier only, not invoked
        allow_delegation=False,
    )

    listener, bus = make_listener_with_bus()
    tools = [FakeWebSearchTool()]

    emit_sync(
        bus,
        AgentExecutionStartedEvent(
            event_id="evt_agent_start",
            agent=agent,
            agent_id="agent_001",
            task=_make_task(),
            tools=tools,
            task_prompt="Find recent papers on agent observability",
        ),
    )
    assert "evt_agent_start" in listener._spans

    emit_sync(
        bus,
        AgentExecutionCompletedEvent(
            event_id="evt_agent_done",
            agent=agent,
            agent_id="agent_001",
            task=_make_task(),
            output="Found 12 relevant papers",
        ),
    )
    assert "evt_agent_start" not in listener._spans


def test_llm_call_with_token_usage():
    from crewai.events import LLMCallStartedEvent, LLMCallCompletedEvent
    from crewai.events.types.llm_events import LLMCallType

    listener, bus = make_listener_with_bus()

    emit_sync(
        bus,
        LLMCallStartedEvent(
            event_id="evt_llm_start",
            call_id="call_xyz",
            model="anthropic/claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            agent_role="researcher",
        ),
    )
    assert "evt_llm_start" in listener._spans

    emit_sync(
        bus,
        LLMCallCompletedEvent(
            event_id="evt_llm_done",
            call_id="call_xyz",
            model="anthropic/claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            response="hello back",
            call_type=LLMCallType.LLM_CALL,
            usage={"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
        ),
    )
    assert "evt_llm_start" not in listener._spans


def test_tool_usage_lifecycle():
    from crewai.events import ToolUsageStartedEvent, ToolUsageFinishedEvent

    listener, bus = make_listener_with_bus()

    emit_sync(
        bus,
        ToolUsageStartedEvent(
            event_id="evt_tool_start",
            tool_name="web_search",
            tool_class="WebSearchTool",
            tool_args={"query": "agent observability"},
            run_attempts=1,
            delegations=0,
            agent_role="researcher",
        ),
    )
    assert "evt_tool_start" in listener._spans

    emit_sync(
        bus,
        ToolUsageFinishedEvent(
            event_id="evt_tool_done",
            tool_name="web_search",
            tool_class="WebSearchTool",
            tool_args={"query": "agent observability"},
            run_attempts=1,
            delegations=0,
            agent_role="researcher",
            started_at=datetime.now(),
            finished_at=datetime.now(),
            from_cache=False,
            output="found 10 results",
        ),
    )
    assert "evt_tool_start" not in listener._spans


def test_parent_child_via_emit_order():
    """The bus auto-correlates parent-child via its scope stack when events nest properly."""
    from crewai.events import (
        TaskStartedEvent,
        TaskCompletedEvent,
        CrewKickoffCompletedEvent,
    )
    from crewai.tasks.task_output import TaskOutput

    listener, bus = make_listener_with_bus()

    # Open crew scope
    emit_sync(bus, _make_crew_started_event(event_id="evt_root"))

    # Open task scope (nested under crew)
    emit_sync(
        bus,
        TaskStartedEvent(
            event_id="evt_child",
            task=_make_task(name="t_research"),
            context="",
        ),
    )

    assert "evt_root" in listener._spans
    assert "evt_child" in listener._spans

    # Close task scope; bus auto-fills started_event_id="evt_child"
    emit_sync(
        bus,
        TaskCompletedEvent(
            event_id="evt_task_done",
            task=_make_task(name="t_research"),
            output=TaskOutput(description="x", raw="done", agent="researcher"),
        ),
    )
    assert "evt_child" not in listener._spans
    assert "evt_root" in listener._spans

    # Close crew scope
    emit_sync(
        bus,
        CrewKickoffCompletedEvent(
            event_id="evt_crew_done",
            crew_name="research_crew",
            output="final",
            total_tokens=500,
        ),
    )
    assert "evt_root" not in listener._spans


def test_failed_event_ends_span_with_error_status():
    from crewai.events import CrewKickoffFailedEvent

    listener, bus = make_listener_with_bus()

    emit_sync(bus, _make_crew_started_event(event_id="evt_will_fail"))
    emit_sync(
        bus,
        CrewKickoffFailedEvent(
            event_id="evt_failed",
            crew_name="research_crew",
            error="LiteLLM rate limit",
        ),
    )
    assert "evt_will_fail" not in listener._spans


def test_instrument_registers_when_package_installed():
    try:
        import crewai  # noqa: F401
    except ImportError:
        pytest.skip("crewai not installed")

    import strathon.instrumentation.crewai as mod

    mod._REGISTERED_LISTENER = None
    client = make_client()
    assert mod.instrument(client) is True
    assert mod._REGISTERED_LISTENER is not None


def test_instrument_is_idempotent():
    try:
        import crewai  # noqa: F401
    except ImportError:
        pytest.skip("crewai not installed")

    import strathon.instrumentation.crewai as mod

    mod._REGISTERED_LISTENER = None
    client1 = make_client()
    mod.instrument(client1)
    first = mod._REGISTERED_LISTENER

    client2 = make_client()
    mod.instrument(client2)
    second = mod._REGISTERED_LISTENER

    assert first is second
    assert second.client is client2

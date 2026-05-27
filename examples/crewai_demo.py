"""Demo: CrewAI auto-instrumentation via the event bus.

Simulates a multi-agent crew workflow (researcher with web_search + LLM calls)
by emitting CrewAI events directly through the event bus. This bypasses the
need for a real LLM API key while still going through the exact same
Strathon instrumentation path that runs in production.

Prerequisites:
    pip install strathon crewai
    Receiver running at http://localhost:4318 (see receiver/README)

Run:
    python crewai_demo.py

Then verify in Postgres:
    psql -U strathon -d strathon -c \
      "SELECT name, agent_name, request_model, tool_name, input_tokens, output_tokens \
       FROM spans ORDER BY start_time_unix_nano;"

For real-world use, just call instrument() on your Strathon Client; every
Crew.kickoff() will emit the same spans automatically. No code changes
needed inside your crew.
"""

from datetime import datetime
from unittest.mock import MagicMock

from crewai import Agent
from crewai.tools import BaseTool
from crewai.events import (
    AgentExecutionCompletedEvent,
    AgentExecutionStartedEvent,
    CrewKickoffCompletedEvent,
    CrewKickoffStartedEvent,
    LLMCallCompletedEvent,
    LLMCallStartedEvent,
    TaskCompletedEvent,
    TaskStartedEvent,
    ToolUsageFinishedEvent,
    ToolUsageStartedEvent,
)
from crewai.events.event_bus import CrewAIEventsBus
from crewai.events.types.llm_events import LLMCallType
from crewai.tasks.task_output import TaskOutput

from strathon import Client
from strathon.instrumentation.crewai import StrathonCrewAIListener


class FakeWebSearchTool(BaseTool):
    name: str = "web_search"
    description: str = "Search the web for information"

    def _run(self, query: str) -> str:
        return "results"


def _emit(bus, event):
    """Emit and wait for the handler thread to finish."""
    future = bus.emit(MagicMock(), event)
    if future is not None:
        future.result(timeout=5)


def _make_task(name):
    t = MagicMock()
    t.configure_mock(name=name)
    t.description = f"description for {name}"
    t.expected_output = "A clear summary of findings"
    return t


def main() -> None:
    client = Client(
        api_key="stra_dev_local_default_project_do_not_use_in_production",
        endpoint="http://localhost:4318",
        service_name="crewai-research-demo",
        environment="dev",
    )

    # Wire the Strathon listener to a CrewAI event bus.
    # In real usage you'd just call strathon.instrument(client, frameworks=["crewai"])
    # and CrewAI's singleton bus picks up the listener automatically.
    listener = StrathonCrewAIListener(client)
    bus = CrewAIEventsBus()
    listener.setup_listeners(bus)

    print("Emitting simulated multi-agent crew workflow...")

    # Set up a real Agent (LLM identifier only; not invoked in this demo)
    agent = Agent(
        role="researcher",
        goal="Find accurate information on agent observability",
        backstory="Senior research analyst with expertise in AI infrastructure",
        llm="gpt-4o-mini",
        allow_delegation=False,
    )

    # Crew kickoff (root span)
    crew_mock = MagicMock()
    crew_mock.agents = [MagicMock(role="researcher")]
    crew_mock.tasks = [_make_task("research_task")]
    crew_mock.process = "sequential"

    _emit(
        bus,
        CrewKickoffStartedEvent(
            event_id="crew_1",
            crew_name="agent_observability_crew",
            crew=crew_mock,
            inputs={"topic": "agent observability"},
        ),
    )

    # Task starts
    task = _make_task("research_task")
    _emit(
        bus,
        TaskStartedEvent(
            event_id="task_1",
            task=task,
            context="user wants a market scan of agent observability platforms",
        ),
    )

    # Agent execution begins
    _emit(
        bus,
        AgentExecutionStartedEvent(
            event_id="agent_1",
            agent=agent,
            agent_id="agent_001",
            task=task,
            tools=[FakeWebSearchTool()],
            task_prompt="Find recent papers and tools for agent observability",
        ),
    )

    # First LLM call (the agent decides to use a tool)
    _emit(
        bus,
        LLMCallStartedEvent(
            event_id="llm_1",
            call_id="call_a",
            model="anthropic/claude-opus-4-7",
            messages=[{"role": "user", "content": "Find papers on agent observability"}],
        ),
    )
    _emit(
        bus,
        LLMCallCompletedEvent(
            event_id="llm_1_done",
            call_id="call_a",
            model="anthropic/claude-opus-4-7",
            messages=[{"role": "user", "content": "Find papers on agent observability"}],
            response="I'll search the web for recent papers.",
            call_type=LLMCallType.LLM_CALL,
            usage={"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
        ),
    )

    # Tool call: web_search
    _emit(
        bus,
        ToolUsageStartedEvent(
            event_id="tool_1",
            tool_name="web_search",
            tool_class="FakeWebSearchTool",
            tool_args={"query": "agent observability 2026"},
            run_attempts=1,
            delegations=0,
        ),
    )
    _emit(
        bus,
        ToolUsageFinishedEvent(
            event_id="tool_1_done",
            tool_name="web_search",
            tool_class="FakeWebSearchTool",
            tool_args={"query": "agent observability 2026"},
            run_attempts=1,
            delegations=0,
            started_at=datetime.now(),
            finished_at=datetime.now(),
            from_cache=False,
            output="Found 10 relevant articles",
        ),
    )

    # Second LLM call (synthesis after tool results)
    _emit(
        bus,
        LLMCallStartedEvent(
            event_id="llm_2",
            call_id="call_b",
            model="anthropic/claude-opus-4-7",
            messages=[{"role": "user", "content": "Synthesize the findings"}],
        ),
    )
    _emit(
        bus,
        LLMCallCompletedEvent(
            event_id="llm_2_done",
            call_id="call_b",
            model="anthropic/claude-opus-4-7",
            messages=[{"role": "user", "content": "Synthesize the findings"}],
            response="Summary of agent observability landscape...",
            call_type=LLMCallType.LLM_CALL,
            usage={"prompt_tokens": 250, "completion_tokens": 350, "total_tokens": 600},
        ),
    )

    # Agent execution completes
    _emit(
        bus,
        AgentExecutionCompletedEvent(
            event_id="agent_1_done",
            agent=agent,
            agent_id="agent_001",
            task=task,
            output="Found 12 papers covering agent observability platforms",
        ),
    )

    # Task completes
    _emit(
        bus,
        TaskCompletedEvent(
            event_id="task_1_done",
            task=task,
            output=TaskOutput(
                description="Research task",
                raw="Final report: agent observability is dominated by ...",
                agent="researcher",
            ),
        ),
    )

    # Crew completes
    _emit(
        bus,
        CrewKickoffCompletedEvent(
            event_id="crew_1_done",
            crew_name="agent_observability_crew",
            output="Final report delivered",
            total_tokens=800,
        ),
    )

    print("Flushing pending spans...")
    client.flush(timeout_millis=15000)
    client.shutdown()
    print("Done. Check the spans table in Postgres.")


if __name__ == "__main__":
    main()

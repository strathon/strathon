"""Demo: OpenAI Agents SDK auto-instrumentation.

Shows a simulated multi-agent workflow (triage -> handoff -> researcher with
tool call) being captured automatically by Strathon. Uses the openai-agents
tracing primitives directly, so no real OpenAI API call is needed.

Prerequisites:
    pip install strathon openai-agents
    Receiver running on http://localhost:4318 (see receiver/README or docker-compose)

Run:
    python agents_demo.py

Then verify in Postgres:
    psql -U strathon -d strathon -c "SELECT name, agent_name, tool_name, input_tokens, output_tokens FROM spans;"
"""

import time

from agents.tracing import (
    agent_span,
    function_span,
    generation_span,
    handoff_span,
    set_trace_processors,
    trace,
)

from strathon import Client
from strathon.instrumentation.openai_agents import StrathonAgentsSDKProcessor


def main() -> None:
    client = Client(
        api_key="stra_dev_local_default_project_do_not_use_in_production",
        endpoint="http://localhost:4318",
        service_name="research-workflow-demo",
        environment="dev",
    )

    # Replace OpenAI Agents SDK's default processors with Strathon's, so we
    # don't need a real OpenAI tracing API key. Use add_trace_processor()
    # instead if you also want OpenAI's built-in dashboard.
    set_trace_processors([StrathonAgentsSDKProcessor(client)])

    print("Emitting simulated multi-agent workflow trace...")

    with trace(workflow_name="research_workflow", group_id="conv_42"):
        # Triage agent decides where to route
        with agent_span(name="triage", handoffs=["researcher", "writer"]):
            with generation_span(model="gpt-5.4") as g:
                g.span_data.usage = {
                    "input_tokens": 80,
                    "output_tokens": 60,
                    "total_tokens": 140,
                }
                time.sleep(0.05)

        # Triage hands off to researcher
        with handoff_span(from_agent="triage", to_agent="researcher"):
            pass

        # Researcher does its work
        with agent_span(name="researcher", tools=["web_search"]):
            with function_span(
                name="web_search",
                input='{"q": "agent observability 2026"}',
            ) as f:
                f.span_data.output = '[{"title": "Strathon launches"}]'
                time.sleep(0.05)

            with generation_span(model="gpt-5.4") as g:
                g.span_data.usage = {
                    "input_tokens": 320,
                    "output_tokens": 480,
                    "total_tokens": 800,
                }
                time.sleep(0.05)

    print("Flushing pending spans...")
    client.flush(timeout_millis=15000)
    client.shutdown()
    print("Done. Check the spans table in Postgres.")


if __name__ == "__main__":
    main()

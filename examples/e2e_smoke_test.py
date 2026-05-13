"""End-to-end smoke test: emit a trace via the SDK, verify it lands in Postgres."""

import time

from strathon import Client


def main() -> None:
    print("Creating Strathon client...")
    client = Client(
        api_key="dev-key-1234",
        endpoint="http://127.0.0.1:4318",
        service_name="e2e-test-agent",
        environment="dev",
        project_id="00000000-0000-0000-0000-000000000001",
    )

    print("Emitting a fake agent trace with nested LLM call and tool call...")

    with client.tracer.start_as_current_span("agent.run") as agent_span:
        agent_span.set_attribute("gen_ai.agent.name", "researcher")
        agent_span.set_attribute("gen_ai.agent.id", "agent-001")
        agent_span.set_attribute("strathon.agent.depth", 0)
        agent_span.set_attribute("gen_ai.workflow.name", "research-workflow")

        # Nested LLM completion
        with client.tracer.start_as_current_span("llm.completion") as llm_span:
            llm_span.set_attribute("gen_ai.operation.name", "chat")
            llm_span.set_attribute("gen_ai.provider.name", "anthropic")
            llm_span.set_attribute("gen_ai.request.model", "claude-opus-4-7")
            llm_span.set_attribute("gen_ai.response.model", "claude-opus-4-7")
            llm_span.set_attribute("gen_ai.usage.input_tokens", 312)
            llm_span.set_attribute("gen_ai.usage.output_tokens", 547)
            time.sleep(0.05)

        # Nested tool call
        with client.tracer.start_as_current_span("tool.call") as tool_span:
            tool_span.set_attribute("gen_ai.tool.name", "web_search")
            tool_span.set_attribute("strathon.tool.arguments", '{"query": "latest LLM papers"}')
            time.sleep(0.05)

    print("Flushing pending spans...")
    flushed = client.flush(timeout_millis=10000)
    print(f"Flush success: {flushed}")

    client.shutdown()
    print("Done.")


if __name__ == "__main__":
    main()

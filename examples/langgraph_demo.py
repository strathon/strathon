"""Demo: LangGraph / LangChain auto-instrumentation.

Simulates a graph-based agent workflow by invoking the Strathon callback
handler with the same shape of arguments LangGraph would pass during a real
graph run. This avoids requiring an actual LLM API key while still going
through the exact same instrumentation path that runs in production.

For real LangGraph usage, just pass the handler in your config:

    from strathon import Client
    from strathon.instrumentation.langgraph import instrument

    client = Client(api_key="...", endpoint="http://localhost:4318")
    handler = instrument(client)

    graph = StateGraph(MessagesState).add_node(...).compile()
    result = graph.invoke(
        {"messages": [HumanMessage(content="...")]},
        config={"callbacks": [handler]},
    )

Prerequisites:
    pip install strathon langgraph langchain-core
    Receiver running at http://localhost:4318

Run:
    python langgraph_demo.py

Verify:
    psql -U strathon -d strathon -c \
      "SELECT name, agent_name, request_model, tool_name, input_tokens, output_tokens \
       FROM spans WHERE attributes->>'strathon.framework'='langgraph' \
       ORDER BY start_time_unix_nano;"
"""

import time
from unittest.mock import MagicMock
from uuid import uuid4

from strathon import Client
from strathon.instrumentation.langgraph import instrument


def _llm_response(prompt_tokens, completion_tokens, model="gpt-4o", text=""):
    """Build a fake LLMResult-shaped object."""
    response = MagicMock()
    response.llm_output = {
        "token_usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "model_name": model,
    }
    gen = MagicMock()
    gen.text = text
    gen.message = None
    gen.generation_info = None
    response.generations = [[gen]]
    return response


def main() -> None:
    client = Client(
        api_key="dev-key",
        endpoint="http://localhost:4318",
        service_name="langgraph-research-demo",
        environment="dev",
    )

    handler = instrument(client)
    if handler is None:
        raise SystemExit("langchain_core not installed. Run: pip install langchain-core")

    print("Emitting simulated LangGraph multi-node workflow...")

    # Root graph invocation
    graph_run = uuid4()
    handler.on_chain_start(
        serialized={"name": "ResearchGraph"},
        inputs={"query": "What's new in agent observability?"},
        run_id=graph_run,
    )

    # Node 1: researcher node calls an LLM that decides to use a tool
    researcher_run = uuid4()
    handler.on_chain_start(
        serialized={"name": "researcher_node"},
        inputs={"messages": [{"role": "user", "content": "research agent observability"}]},
        run_id=researcher_run,
        parent_run_id=graph_run,
        metadata={"langgraph_node": "researcher", "langgraph_step": 1},
    )

    # LLM call inside researcher node — decides to call web_search
    llm1 = uuid4()
    handler.on_chat_model_start(
        serialized={"kwargs": {"model": "anthropic/claude-opus-4-7"}},
        messages=[[MagicMock(content="research agent observability", type="human")]],
        run_id=llm1,
        parent_run_id=researcher_run,
    )
    time.sleep(0.02)
    handler.on_llm_end(
        response=_llm_response(120, 60, model="anthropic/claude-opus-4-7", text="I'll search."),
        run_id=llm1,
    )

    # Tool call: web_search
    tool1 = uuid4()
    handler.on_tool_start(
        serialized={"name": "web_search"},
        input_str='{"query":"agent observability 2026"}',
        run_id=tool1,
        parent_run_id=researcher_run,
        inputs={"query": "agent observability 2026"},
    )
    time.sleep(0.03)
    handler.on_tool_end(output="10 articles found", run_id=tool1)

    # Second LLM call: synthesize tool results
    llm2 = uuid4()
    handler.on_chat_model_start(
        serialized={"kwargs": {"model": "anthropic/claude-opus-4-7"}},
        messages=[[MagicMock(content="synthesize", type="human")]],
        run_id=llm2,
        parent_run_id=researcher_run,
    )
    time.sleep(0.02)
    handler.on_llm_end(
        response=_llm_response(320, 480, model="anthropic/claude-opus-4-7", text="Summary..."),
        run_id=llm2,
    )

    # Researcher node ends
    handler.on_chain_end(outputs={"findings": "12 papers"}, run_id=researcher_run)

    # Node 2: writer node
    writer_run = uuid4()
    handler.on_chain_start(
        serialized={"name": "writer_node"},
        inputs={"findings": "12 papers"},
        run_id=writer_run,
        parent_run_id=graph_run,
        metadata={"langgraph_node": "writer", "langgraph_step": 2},
    )

    # Writer LLM call
    llm3 = uuid4()
    handler.on_chat_model_start(
        serialized={"kwargs": {"model": "anthropic/claude-opus-4-7"}},
        messages=[[MagicMock(content="write the report", type="human")]],
        run_id=llm3,
        parent_run_id=writer_run,
    )
    time.sleep(0.02)
    handler.on_llm_end(
        response=_llm_response(450, 600, model="anthropic/claude-opus-4-7", text="Final report"),
        run_id=llm3,
    )

    handler.on_chain_end(outputs={"report": "Final report"}, run_id=writer_run)

    # Root graph ends
    handler.on_chain_end(
        outputs={"final": "Research complete"},
        run_id=graph_run,
    )

    print("Flushing pending spans...")
    client.flush(timeout_millis=15000)
    client.shutdown()
    print("Done.")


if __name__ == "__main__":
    main()

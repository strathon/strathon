# LangChain Integration

Strathon integrates with LangChain via `BaseCallbackHandler`, the same
handler used for LangGraph. Chains, agents, tools, and LLM calls are
all traced automatically.

## Installation

```bash
pip install strathon[langchain]
```

## Setup

```python
from strathon import Client, instrument

client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
)
instrument(client, frameworks=["langchain"])
```

## What Gets Captured

- **Chain runs**: start, end, error
- **LLM calls**: model, tokens, latency, prompt/completion
- **Tool calls**: tool name, arguments, return value
- **Retriever calls**: query, document count, sources

Each chain invocation creates a trace with nested spans.

## Example Policy

Block tool calls to unapproved external APIs:

```cel
attrs["gen_ai.tool.name"] == "requests_get"
  && !attrs["gen_ai.tool.args"].contains("api.internal.com")
```

Throttle retrieval calls to prevent runaway RAG loops:

```cel
attrs["gen_ai.tool.name"] == "vector_search"
```

With action `throttle` and a rate limit, Strathon caps the call frequency.

## Notes

- Shares the handler with LangGraph. Instrumenting one covers both.
- Works with LangChain 0.3+ and LangChain Community packages.
- Callback handlers are attached automatically via `instrument()`.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)
- [LangChain documentation](https://python.langchain.com/)

# LangChain Integration

Strathon evaluates every LangChain tool call against your policies before
it executes: a matched `block` or `throttle` stops the call at the callback
boundary. Chains, agents, tools, and LLM calls run through the same
`BaseCallbackHandler` used for LangGraph, which you attach to each invocation.

> **Enforcement scope:** the LangChain callback surface is synchronous.
> `block` and `throttle` enforce (the tool never runs); `steer` is recorded
> but the original tool still runs; `require_approval` **fails closed** (the
> call is blocked and recorded) because a sync callback cannot pause for a
> human decision. For steer substitution or interactive approval, use
> `enforce_steer` (tool-invoke wrapping). Full picture in the
> [approval matrix](https://getstrathon.com/docs/intervention#approval-support).


## Installation

```bash
pip install "strathon[langchain]"
```

## Setup

```python
from strathon import Client
from strathon.instrumentation.langchain import instrument

client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
)

# instrument() returns a LangChain callback handler. Strathon enforces and
# traces through it, so attach it to every chain or agent invocation.
handler = instrument(client)

result = chain.invoke(
    {"input": "..."},
    config={"callbacks": [handler]},
)
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
  && !attrs["strathon.tool.args"].contains("api.internal.com")
```

Throttle retrieval calls to prevent runaway RAG loops:

```cel
attrs["gen_ai.tool.name"] == "vector_search"
```

With action `throttle` and a rate limit, Strathon caps the call frequency.

## Notes

- Shares the handler with LangGraph; one handler covers both.
- The handler must be passed on every invocation via
  `config={"callbacks": [handler]}`. LangChain has no global callback
  registry, so an unattached handler does nothing: no spans, no enforcement.
- Requires `langchain-core>=0.3.0` (installed by the `langchain` extra); works
  with LangChain 0.3+ and LangChain Community packages.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)
- [LangChain documentation](https://python.langchain.com/)

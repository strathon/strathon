# Anthropic Integration

Strathon instruments the Anthropic Python SDK by wrapping
`messages.create`. Every Claude call is traced with model parameters,
token usage, and response content.

> **Enforcement scope:** this integration is observability-only. It wraps
> the LLM call (`messages.create`), not tool execution, so it records what
> the model does but does not block, throttle, steer, or gate tool calls.
> Use it for tracing, `log`, and `alert` policies. To *enforce* policies on
> tool calls (block / throttle / steer / require_approval), instrument the
> agent framework that runs the tools — for example the
> [Claude Agent SDK](https://getstrathon.com/docs/frameworks/claude-agent-sdk),
> [LangGraph](https://getstrathon.com/docs/frameworks/langgraph), or another
> tool-executing integration.

## Installation

```bash
pip install strathon[anthropic]
```

## Setup

```python
from strathon import Client, instrument

client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
)
instrument(client, frameworks=["anthropic"])

# Use the Anthropic SDK as normal.
import anthropic

client = anthropic.Anthropic()
message = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}],
)
```

## What Gets Captured

- **Model**: name, provider
- **Token usage**: input tokens, output tokens
- **Latency**: request duration
- **Messages**: user and assistant messages
- **Tool use**: the tool calls the model *requests* (name, arguments) as
  seen in the LLM response — not the tool's actual execution

## Example Policy

Because this surface is observability-only, use it with `log` or `alert`
actions rather than blocking actions.

Alert on calls to an expensive model so you can watch spend:

```cel
attrs["gen_ai.request.model"].contains("opus")
```

Log all Claude API calls for audit purposes:

```cel
attrs["gen_ai.system"] == "anthropic"
```

## Notes

- Wraps `messages.create` (sync and async).
- Streaming responses are traced with token counts at completion.
- Requires `anthropic>=0.40.0` (installed by the `anthropic` extra).

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)
- [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python)

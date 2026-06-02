# Anthropic Integration

Strathon instruments the Anthropic Python SDK by wrapping
`messages.create`. Every Claude call is traced with model parameters,
token usage, and response content.

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
- **Tool use**: tool name, arguments, results (if using tools)

## Example Policy

Require approval for tool calls that modify data:

```cel
attrs["gen_ai.tool.name"] in ["update_database", "delete_record"]
```

Log all Claude API calls for audit purposes:

```cel
attrs["gen_ai.system"] == "anthropic"
```

## Notes

- Wraps `messages.create` (sync and async).
- Streaming responses are traced with token counts at completion.
- Works with `anthropic` Python SDK 0.30+.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)
- [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python)

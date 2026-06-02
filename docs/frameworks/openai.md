# OpenAI Integration

Strathon instruments the OpenAI Python SDK by wrapping
`chat.completions.create`. Every LLM call is traced with full model
parameters, token usage, and response content.

## Installation

```bash
pip install strathon[openai]
```

## Setup

```python
from strathon import Client, instrument

client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
)
instrument(client, frameworks=["openai"])

# Use the OpenAI SDK as normal — Strathon traces automatically.
import openai

response = openai.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Hello"}],
)
```

## What Gets Captured

- **Model**: name, provider
- **Token usage**: prompt tokens, completion tokens, total
- **Latency**: request duration
- **Messages**: system, user, assistant messages
- **Function/tool calls**: name, arguments (if using tools)

## Example Policy

Block requests to expensive models in development:

```cel
attrs["gen_ai.request.model"] == "gpt-4o"
  && attrs["strathon.project.environment"] == "development"
```

Alert on high token usage:

```cel
attrs["gen_ai.response.total_tokens"] > 10000
```

## Notes

- The wrapper intercepts `chat.completions.create` (sync and async).
- Streaming responses are traced with token counts at completion.
- Works with `openai` Python SDK 1.0+.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)
- [OpenAI Python SDK](https://github.com/openai/openai-python)

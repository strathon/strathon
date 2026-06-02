# Pydantic AI Integration

Strathon integrates with Pydantic AI via its `AbstractCapability`
plugin system. This is a first-class integration — no monkey-patching.

## Installation

```bash
pip install strathon[pydantic-ai]
```

## Setup

```python
from strathon import Client, instrument

client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
)
instrument(client, frameworks=["pydantic_ai"])
```

The integration registers a `StrathonFirewall` capability that evaluates
policies on every tool call and LLM interaction.

## What Gets Captured

- **Agent runs**: model, system prompt, result type
- **Tool calls**: tool name, arguments, return value, validation
- **LLM calls**: model, tokens, latency
- **Structured output**: Pydantic model validation results

## Example Policy

Block tools that send external network requests:

```cel
attrs["gen_ai.tool.name"] in ["http_get", "http_post", "fetch_url"]
```

Alert when structured output validation fails:

```cel
attrs["pydantic_ai.validation_error"] == true
```

## Notes

- Uses `AbstractCapability` — Pydantic AI's official plugin interface.
- Zero monkey-patching. The capability is registered at instrument time.
- Works with Pydantic AI 0.1+.
- 36 tests cover the integration.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)
- [Pydantic AI documentation](https://ai.pydantic.dev/)

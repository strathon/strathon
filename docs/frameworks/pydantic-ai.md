# Pydantic AI Integration

Strathon enforces policies on Pydantic AI tool calls before they execute:
`block` and `throttle` raise, and `steer` returns the replacement in place of
the real result. Integration is a first-class `AbstractCapability`
you pass in the agent's `capabilities` list, no monkey-patching.

> **Enforcement scope:** Pydantic AI is instrumented through a synchronous
> pre-execution hook that can short-circuit the tool call. It enforces
> `block` and `throttle` (which raise) and `steer` (the hook returns the
> replacement in place of the real result; the tool body never runs).
> `require_approval` **fails closed** (the call is blocked and recorded)
> because a sync hook cannot pause for a human decision. For interactive
> approval, use `enforce_steer` (tool-invoke wrapping) or a framework whose
> pre-execution hook is async. See the
> [approval matrix](https://getstrathon.com/docs/intervention#approval-support).

## Installation

```bash
pip install "strathon[pydantic-ai]"
```

## Setup

```python
from strathon import Client
from strathon.instrumentation.pydantic_ai import create_firewall
from pydantic_ai import Agent

client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
)

# create_firewall() returns a Pydantic AI capability. Pass it in the agent's
# capabilities list; the capability is what evaluates policies on every tool
# call and LLM interaction.
firewall = create_firewall(client)

agent = Agent("openai:gpt-4o", capabilities=[firewall])
```

The `StrathonFirewall` capability you pass to the agent evaluates policies on
every tool call and LLM interaction.

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

Require approval before a sensitive tool runs (replace `fetch_record` with
your tool's name):

```cel
attrs["gen_ai.tool.name"] == "fetch_record"
```

## Notes

- Uses `AbstractCapability`: Pydantic AI's official plugin interface.
- Zero monkey-patching. You pass the capability in `Agent(capabilities=[...])`.
- Requires `pydantic-ai-slim>=1.80.0` (installed by the `pydantic-ai` extra).
- 36 tests cover the integration.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)
- [Pydantic AI documentation](https://ai.pydantic.dev/)

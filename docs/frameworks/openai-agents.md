# OpenAI Agents SDK Integration

Strathon integrates with the OpenAI Agents SDK via its official
`TracingProcessor` extension point, capturing the full span lifecycle
including tool calls, handoffs, and guardrail evaluations.

## Installation

```bash
pip install strathon[openai-agents]
```

## Setup

```python
from strathon import Client, instrument

client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
)
instrument(client, frameworks=["openai_agents"])
```

## What Gets Captured

- **Agent runs**: start, handoffs, completion
- **Tool calls**: function name, arguments, return value
- **Guardrail evaluations**: pass/fail with context
- **LLM calls**: model, tokens, latency
- **Handoffs**: source agent, target agent, reason

## Example Policy

Block shell command execution:

```cel
attrs["gen_ai.tool.name"] == "run_command"
  && attrs["strathon.tool.args"].contains("rm ")
```

Log all tool calls for a specific agent:

```cel
attrs["gen_ai.agent.name"] == "research_agent"
```

## Notes

- Uses the official `TracingProcessor` protocol. No monkey-patching.
- Compatible with OpenAI Agents SDK 0.1+.
- Guardrail results are captured as span events.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)
- [OpenAI Agents SDK docs](https://openai.github.io/openai-agents-python/)

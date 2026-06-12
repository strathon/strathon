# OpenAI Agents SDK Integration

Strathon enforces policies on OpenAI Agents SDK tool calls before they
execute, with the full action set including interactive approval. Tracing
rides the official `TracingProcessor` extension point, capturing the span
lifecycle including handoffs and guardrail evaluations; enforcement injects
run hooks through the SDK's documented `Runner` entry points.

> **Enforcement scope:** full. The pre-execution hook is async, so all seven
> actions enforce: `block` and `throttle` stop the call, `steer` substitutes
> the tool result directly, and `require_approval` pauses until an operator
> decides (and fails closed on expiry).


## Installation

```bash
pip install "strathon[openai-agents]"
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

- Tracing uses the official `TracingProcessor` protocol. Enforcement wraps
  `Runner.run` / `run_sync` / `run_streamed` to inject Strathon `RunHooks`:
  a wrap of the framework's documented entry point, since the SDK exposes no
  pre-execution policy hook of its own.
- Requires `openai-agents>=0.6.0` (installed by the `openai-agents` extra).
- Guardrail results are captured as span events.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)
- [OpenAI Agents SDK docs](https://openai.github.io/openai-agents-python/)

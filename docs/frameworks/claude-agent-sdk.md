# Claude Agent SDK Integration

Strathon integrates with the Claude Agent SDK by wrapping the `query()`
method, capturing tool use, agent reasoning, and response content.

## Installation

```bash
pip install strathon[claude-agent]
```

## Setup

```python
from strathon import Client, instrument

client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
)
instrument(client, frameworks=["claude_agent"])
```

## What Gets Captured

- **Agent queries**: input, model, parameters
- **Tool use**: tool name, arguments, results
- **Agent reasoning**: thinking content (if extended thinking enabled)
- **Token usage**: input tokens, output tokens
- **Latency**: per-query timing

## Example Policy

Block tool calls that access sensitive directories:

```cel
attrs["gen_ai.tool.name"] == "file_read"
  && attrs["gen_ai.tool.args"].contains("/etc/")
```

Require approval for agent actions that modify infrastructure:

```cel
attrs["gen_ai.tool.name"] in ["deploy", "rollback", "scale"]
```

## Notes

- Wraps `query()` for tracing and policy enforcement.
- Captures extended thinking content as span attributes.
- Works with Claude Agent SDK 0.1+.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)

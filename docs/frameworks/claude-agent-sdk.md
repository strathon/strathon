# Claude Agent SDK Integration

Strathon enforces policies on Claude Agent SDK tool calls before they
execute, with the full action set including interactive approval.
Enforcement uses the SDK's first-class `PreToolUse`/`PostToolUse` hooks on
`ClaudeSDKClient`; a `query()` wrapper adds observability for code not using
the client.

> **Enforcement scope:** full on `ClaudeSDKClient`, where the async hooks
> enforce all seven actions, including interactive `require_approval`. The
> module-level `query()` function does not support hooks, so that path is
> observability-only.


## Installation

```bash
pip install "strathon[claude-agent]"
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
  && attrs["strathon.tool.args"].contains("/etc/")
```

Require approval for agent actions that modify infrastructure:

```cel
attrs["gen_ai.tool.name"] in ["deploy", "rollback", "scale"]
```

## Notes

- Wraps `query()` for tracing and policy enforcement.
- Captures extended thinking content as span attributes.
- Requires `claude-agent-sdk>=0.1.0` (installed by the `claude-agent` extra).

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)

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
from strathon import Client
from strathon.instrumentation.claude_agent import create_strathon_hooks
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
)

# create_strathon_hooks() returns PreToolUse/PostToolUse hooks. Pass them to
# ClaudeAgentOptions; the PreToolUse hook is what evaluates policies and denies
# blocked tool calls. Hooks require ClaudeSDKClient (the module-level query()
# function does not support hooks).
hooks = create_strathon_hooks(client)
options = ClaudeAgentOptions(hooks=hooks)

async with ClaudeSDKClient(options=options) as agent:
    await agent.query("...")
    async for message in agent.receive_response():
        ...
```

For session-level tracing of code that uses the module-level `query()`
function, also call `instrument(client, frameworks=["claude_agent"])`; that
path is observability-only (no enforcement), since `query()` does not run
hooks.

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

- Tool-level enforcement runs through the `PreToolUse` hook on
  `ClaudeSDKClient`; `query()` and `ClaudeSDKClient.query()` are wrapped for
  session-level tracing only.
- Captures extended thinking content as span attributes.
- Requires `claude-agent-sdk>=0.1.81` (installed by the `claude-agent`
  extra) — the minimum version with the `PreToolUse`/`PostToolUse` hooks used
  for enforcement.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)

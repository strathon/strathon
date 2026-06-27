# AutoGen Integration

Strathon enforces policies on AutoGen tool calls before they execute,
across multi-agent conversations, with the full action set including
interactive approval. Integration wraps `BaseChatAgent.on_messages` and
captures conversations, tool calls, and LLM interactions.

> **Enforcement scope:** full. The pre-execution hook is async, so all seven
> actions enforce: `block` and `throttle` stop the call, `steer` substitutes
> the tool result directly, and `require_approval` pauses until an operator
> decides (and fails closed on expiry).


## Installation

```bash
pip install "strathon[autogen]"
```

## Setup

```python
from strathon import Client, instrument

client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
)
instrument(client, frameworks=["autogen"])
```

## What Gets Captured

- **Agent messages**: sender, receiver, content
- **Tool calls**: function name, arguments, return value
- **Multi-agent conversations**: full message thread
- **LLM calls**: model, tokens, latency

## Example Policy

Block code execution tools in production:

```cel
attrs["gen_ai.tool.name"] == "execute_code"
```

Block code execution in production (AutoGen group chats re-send the full
history every turn, so a runaway loop is expensive: gate the dangerous tool):

```cel
attrs["gen_ai.tool.name"] == "execute_code"
  && attrs["strathon.project.environment"] == "production"
```

## Notes

- Wraps `BaseChatAgent.on_messages` and `BaseGroupChat.run` for conversation
  and team tracing, and `BaseTool.run_json` to enforce policies on each tool
  call (the enforcement boundary). All installed at instrument time.
- Requires `autogen-agentchat>=0.7.0` (installed by the `autogen` extra).
- Multi-agent conversations create a single trace with per-agent spans.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)
- [AutoGen documentation](https://microsoft.github.io/autogen/)

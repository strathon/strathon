# AutoGen Integration

Strathon integrates with Microsoft AutoGen by wrapping
`BaseChatAgent.on_messages`, capturing multi-agent conversations,
tool calls, and LLM interactions.

## Installation

```bash
pip install strathon[autogen]
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
  && attrs["strathon.project.environment"] == "production"
```

Limit conversation depth to prevent infinite agent loops:

```cel
attrs["autogen.message_count"] > 50
```

## Notes

- Wraps `BaseChatAgent.on_messages` for message interception.
- Works with AutoGen 0.4+.
- Multi-agent conversations create a single trace with per-agent spans.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)
- [AutoGen documentation](https://microsoft.github.io/autogen/)

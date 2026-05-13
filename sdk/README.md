# Strathon SDK

Python SDK for [Strathon](https://github.com/strathon/strathon), the open-source observability and runtime control platform for AI agents.

## Install

```bash
pip install strathon
```

Optional framework integrations:

```bash
pip install "strathon[openai-agents]"   # OpenAI Agents SDK
pip install "strathon[claude-agent]"    # Claude Agent SDK
pip install "strathon[langchain]"       # LangChain
pip install "strathon[crewai]"          # CrewAI
pip install "strathon[autogen]"         # AutoGen
pip install "strathon[all]"             # All integrations
```

## Quick start

```python
from strathon import Client, instrument

client = Client(
    api_key="your-api-key",
    endpoint="http://localhost:4318",  # Self-hosted Strathon
)

instrument(client, frameworks=["openai_agents", "anthropic"])

# Your agent code runs as normal; Strathon captures traces automatically.
```

## What gets captured

- Every LLM call: model, tokens in/out, cost, latency
- Tool calls: name, arguments, result, duration
- Agent topology: parent-child relationships, sub-agent spawns, handoffs
- Decision branches: which path the agent took and why
- Errors: failed tool calls, model errors, timeouts

## Status

v0.1.0 in active development. Target stable release: June 2026.

## License

MIT

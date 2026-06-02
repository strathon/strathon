# Strathon SDK

Python SDK for [Strathon](https://github.com/strathon/strathon), the open-source AI agent firewall.

## Install

```bash
pip install strathon
```

Optional framework integrations:

```bash
pip install "strathon[openai]"          # OpenAI
pip install "strathon[openai-agents]"   # OpenAI Agents SDK
pip install "strathon[anthropic]"       # Anthropic
pip install "strathon[claude-agent]"    # Claude Agent SDK
pip install "strathon[langgraph]"       # LangGraph
pip install "strathon[langchain]"       # LangChain
pip install "strathon[crewai]"          # CrewAI
pip install "strathon[autogen]"         # AutoGen
pip install "strathon[pydantic-ai]"     # Pydantic AI
pip install "strathon[google-adk]"      # Google ADK
pip install "strathon[all]"             # All 10 frameworks
```

## Quick start

```python
from strathon import Client, instrument

client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
)
instrument(client, frameworks=["openai"])

# Your agent code runs as normal. Strathon traces every call
# and enforces CEL policies before tool execution.
```

## What gets captured

- Every LLM call: model, tokens in/out, cost, latency
- Tool calls: name, arguments, result, duration
- Agent topology: parent-child relationships, handoffs
- Errors: failed tool calls, model errors, timeouts

## Documentation

- [Quickstart](https://getstrathon.com/docs/quickstart)
- [Framework guides](https://getstrathon.com/docs/frameworks)
- [CEL policy reference](https://getstrathon.com/docs/cel-reference)
- [GitHub](https://github.com/strathon/strathon)

## License

Apache License 2.0. See [LICENSE](https://github.com/strathon/strathon/blob/main/LICENSE).

# Framework Integrations

Strathon instruments your agent framework's own extension points — callback
handlers, plugins, event listeners — so connecting it takes two lines and no
changes to your agent logic. All 10 integrations use first-class extension
points where available. No monkey-patching.

```bash
pip install strathon[langgraph]   # one framework
pip install strathon[all]         # all 10
```

Then in your code:

```python
from strathon import Client, instrument

client = Client(api_key="stra_...", endpoint="http://localhost:4318")
instrument(client, frameworks=["langgraph"])
```

## Supported frameworks

| Framework | Integration | Guide |
|-----------|-------------|-------|
| **LangGraph** | LangChain `BaseCallbackHandler` — intercepts tool calls before execution | [langgraph.md](langgraph.md) |
| **CrewAI** | Event listener on the CrewAI event bus | [crewai.md](crewai.md) |
| **LangChain** | Same callback handler as LangGraph | [langchain.md](langchain.md) |
| **OpenAI Agents SDK** | `TracingProcessor` extension point | [openai-agents.md](openai-agents.md) |
| **Google ADK** | First-class `BasePlugin` | [google-adk.md](google-adk.md) |
| **Pydantic AI** | First-class `AbstractCapability` | [pydantic-ai.md](pydantic-ai.md) |
| **Claude Agent SDK** | `query()` wrapper | [claude-agent-sdk.md](claude-agent-sdk.md) |
| **AutoGen** | `BaseChatAgent.on_messages` wrapper | [autogen.md](autogen.md) |
| **OpenAI** | Drop-in wrapper for `chat.completions.create` | [openai.md](openai.md) |
| **Anthropic** | Drop-in wrapper for `messages.create` | [anthropic.md](anthropic.md) |

## Which should I use?

If you are building production agents and not already committed to a framework,
LangGraph is the most widely deployed choice for stateful, auditable workflows
and is the best-supported integration here. If you already use a framework,
pick its guide above — every integration captures the same trace data and
enforces the same policies.

The raw model-SDK wrappers (OpenAI, Anthropic) instrument direct model calls
rather than an agent framework. Use them when you call a model SDK directly
without an orchestration layer.

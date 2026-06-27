# Framework Integrations

Strathon instruments your agent framework's own extension points — callback
handlers, plugins, hooks, event listeners. Most integrations connect with a
single `instrument()` call; the callback-, plugin-, hook-, and capability-based
frameworks (LangGraph, LangChain, Google ADK, Claude Agent SDK, Pydantic AI)
take one extra line to attach the handler, plugin, hooks, or capability — each
guide shows the exact pattern. None of it changes your agent logic.

```bash
pip install "strathon[langgraph]"   # one framework
pip install "strathon[all]"         # all 10
```

Then in your code:

```python
from strathon import Client
from strathon.instrumentation.langgraph import instrument

client = Client(api_key="stra_...", endpoint="http://localhost:4318")
handler = instrument(client)

# LangGraph returns a callback handler — attach it on each invocation:
result = agent.invoke(inputs, config={"callbacks": [handler]})
```

That is the LangGraph pattern; CrewAI, the OpenAI Agents SDK, and AutoGen need
only `instrument(client, frameworks=["..."])`, while Google ADK, the Claude
Agent SDK, and Pydantic AI each take a one-time wiring step shown in their guide.

## Supported frameworks

| Framework | Integration | Guide |
|-----------|-------------|-------|
| **LangGraph** | LangChain `BaseCallbackHandler`: intercepts tool calls before execution | [Guide](langgraph.md) |
| **CrewAI** | Event listener (tracing) + tool-invoke wrapping (enforcement) | [Guide](crewai.md) |
| **LangChain** | Same callback handler as LangGraph | [Guide](langchain.md) |
| **OpenAI Agents SDK** | `TracingProcessor` (tracing) + `RunHooks` (enforcement) | [Guide](openai-agents.md) |
| **Google ADK** | First-class `BasePlugin` | [Guide](google-adk.md) |
| **Pydantic AI** | First-class `AbstractCapability` | [Guide](pydantic-ai.md) |
| **Claude Agent SDK** | `PreToolUse`/`PostToolUse` hooks (tracing via `query()` wrapper) | [Guide](claude-agent-sdk.md) |
| **AutoGen** | `BaseTool.run_json` wrapper (tool enforcement) + conversation tracing | [Guide](autogen.md) |
| **OpenAI** | Drop-in wrapper for `chat.completions.create` | [Guide](openai.md) |
| **Anthropic** | Drop-in wrapper for `messages.create` | [Guide](anthropic.md) |

## Which should I use?

If you are building production agents and not already committed to a framework,
LangGraph is the most widely deployed choice for stateful, auditable workflows
and is the best-supported integration here. If you already use a framework,
pick its guide above; every integration captures the same trace data and
enforces the same policies.

The raw model-SDK wrappers (OpenAI, Anthropic) instrument direct model calls
rather than an agent framework. Use them when you call a model SDK directly
without an orchestration layer.

# LangGraph Integration

Strathon evaluates every LangGraph tool call against your policies before
it executes: a matched `block` or `throttle` stops the call at the callback
boundary, before the tool body runs. Integration uses LangChain's
`BaseCallbackHandler`, which you attach to each graph invocation.

> **Enforcement scope:** the LangChain callback surface is synchronous.
> `block` and `throttle` enforce (the tool never runs); `steer` is recorded
> but the original tool still runs; `require_approval` **fails closed** (the
> call is blocked and recorded) because a sync callback cannot pause for a
> human decision. For steer substitution or interactive approval, use
> `enforce_steer` (tool-invoke wrapping). Full picture in the
> [approval matrix](https://getstrathon.com/docs/intervention#approval-support).


## Installation

```bash
pip install "strathon[langgraph]"
```

## Setup

```python
from strathon import Client
from strathon.instrumentation.langgraph import instrument

client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
)

# instrument() returns a LangChain callback handler. Strathon enforces and
# traces through it, so attach it to every graph invocation.
handler = instrument(client)

result = agent.invoke(
    {"messages": [{"role": "user", "content": "..."}]},
    config={"callbacks": [handler]},
)
```

The handler intercepts every `StateGraph` tool call: matched `block` and
`throttle` policies raise before the tool body runs, and each call is traced.
Pass the same handler on every invocation, including `ainvoke` and `stream`.

## What Gets Captured

- **LLM calls**: model, tokens, latency, prompt/completion
- **Tool calls**: tool name, arguments, return value
- **Graph state transitions**: node entries/exits, edge routing
- **Errors**: exceptions with full stack trace context

Each graph invocation creates a trace with nested spans for every
node execution and tool call.

## Example Policy

Block any tool call that attempts to access the filesystem:

```cel
attrs["gen_ai.tool.name"] in ["read_file", "write_file", "delete_file"]
```

Apply this as a `block` action. When a LangGraph agent tries to invoke
`read_file`, Strathon raises `StrathonPolicyBlocked` before the tool
function body executes.

## Approval Workflow

For high-risk tool calls you can use `require_approval`:

```cel
attrs["gen_ai.tool.name"] == "execute_sql"
  && attrs["strathon.tool.args"].contains("DELETE")
```

> **How approval behaves on LangGraph:** LangGraph is instrumented through a
> synchronous callback (`on_tool_start`), which cannot suspend execution to
> wait for a human. So a matched `require_approval` policy **fails closed**:
> the tool call is blocked (raising `StrathonPolicyBlocked`) and the
> intervention is recorded, rather than pausing for an interactive decision.
> For interactive approval that pauses until an operator responds, use a
> tool-invoke surface that supports it: `enforce_steer`,
> or a framework whose pre-execution hook is async (for
> example the [OpenAI Agents SDK](https://getstrathon.com/docs/frameworks/openai-agents),
> [AutoGen](https://getstrathon.com/docs/frameworks/autogen),
> [Google ADK](https://getstrathon.com/docs/frameworks/google-adk),
> [Claude Agent SDK](https://getstrathon.com/docs/frameworks/claude-agent-sdk),
> or [CrewAI](https://getstrathon.com/docs/frameworks/crewai)). See the
> [approval matrix](https://getstrathon.com/docs/intervention#approval-support)
> for which surfaces pause versus fail closed.

## Steer Example

Redirect a tool call to a sandboxed version:

```cel
attrs["gen_ai.tool.name"] == "web_search"
```

> **How steer behaves on LangGraph:** because the `on_tool_start` callback
> cannot substitute a tool's return value, `steer` on this surface is
> observe-only; the match is recorded (a `strathon.policy.steered` span)
> but the original tool still runs. To actually replace a tool call with a
> safer alternative, use a tool-invoke surface:
> `enforce_steer`, or a framework whose hook controls the return value (for
> example CrewAI or the async agent SDKs). For hard prevention on LangGraph,
> use `block` instead.

## Notes

- LangGraph and LangChain share the same callback handler. If you
  instrument `langgraph`, LangChain chains also get traced.
- The handler must be passed on every invocation via
  `config={"callbacks": [handler]}`. LangChain has no global callback
  registry, so a handler that is built but never attached does nothing: no
  spans, no enforcement.
- Requires `langchain-core>=0.3.0` (installed by the `langgraph` extra); works with current LangGraph and LangChain 0.3+ releases.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)
- [LangGraph documentation](https://langchain-ai.github.io/langgraph/)

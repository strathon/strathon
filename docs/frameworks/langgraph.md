# LangGraph Integration

Strathon integrates with LangGraph via LangChain's `BaseCallbackHandler`,
intercepting tool calls before execution and capturing full trace context.

## Installation

```bash
pip install strathon[langgraph]
```

## Setup

```python
from strathon import Client, instrument

client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
)
instrument(client, frameworks=["langgraph"])
```

Once instrumented, every `StateGraph` invocation is traced automatically.
Tool calls pass through the policy engine before executing.

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
> wait for a human. So a matched `require_approval` policy **fails closed** —
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
> observe-only — the match is recorded (a `strathon.policy.steered` span)
> but the original tool still runs. To actually replace a tool call with a
> safer alternative, use a tool-invoke surface:
> `enforce_steer`, or a framework whose hook controls the return value (for
> example CrewAI or the async agent SDKs). For hard prevention on LangGraph,
> use `block` instead.

## Notes

- LangGraph and LangChain share the same callback handler. If you
  instrument `langgraph`, LangChain chains also get traced.
- The handler attaches via `config={"callbacks": [strathon_handler]}`.
  `instrument()` patches this automatically.
- Requires `langchain-core>=0.3.0` (installed by the `langgraph` extra); works with current LangGraph and LangChain 0.3+ releases.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)
- [LangGraph documentation](https://langchain-ai.github.io/langgraph/)

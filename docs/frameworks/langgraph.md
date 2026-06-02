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

For high-risk tool calls, use `require_approval` instead of `block`:

```cel
attrs["gen_ai.tool.name"] == "execute_sql"
  && attrs["gen_ai.tool.args"].contains("DELETE")
```

The agent pauses until an operator approves in the dashboard or Slack.

## Steer Example

Redirect a tool call to a sandboxed version:

```cel
attrs["gen_ai.tool.name"] == "web_search"
```

With action `steer` and steer target `sandboxed_web_search`, the agent
uses the safer alternative automatically.

## Notes

- LangGraph and LangChain share the same callback handler. If you
  instrument `langgraph`, LangChain chains also get traced.
- The handler attaches via `config={"callbacks": [strathon_handler]}`.
  `instrument()` patches this automatically.
- Works with LangGraph 0.2+ and LangChain 0.3+.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)
- [LangGraph documentation](https://langchain-ai.github.io/langgraph/)

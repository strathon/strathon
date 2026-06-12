# CEL Policy Reference

Strathon uses [CEL (Common Expression Language)](https://cel.dev) for policy rules. CEL is Google's open standard used in Firebase, Kubernetes, and Envoy.

## Quick Reference

### Comparisons
```cel
attrs["gen_ai.tool.name"] == "search"           // exact match
attrs["gen_ai.tool.name"] != "shell_exec"       // not equal
attrs["gen_ai.usage.cost"] > 0.50               // greater than
attrs["gen_ai.usage.cost"] <= 1.00              // less or equal
```

### Logical operators
```cel
// AND — both conditions must be true
attrs["gen_ai.tool.name"] == "delete" && attrs["gen_ai.agent.name"] == "cleanup-bot"

// OR — either condition
attrs["gen_ai.tool.name"] == "rm" || attrs["gen_ai.tool.name"] == "drop"

// NOT — negate
!(attrs["gen_ai.tool.name"] in ["search", "read", "summarize"])
```

### String matching
```cel
// Regex match
attrs["gen_ai.content"].matches("(?i)ignore previous instructions")

// Contains substring
attrs["gen_ai.content"].contains("password")

// Starts with
attrs["gen_ai.tool.name"].startsWith("db.")

// Ends with
attrs["gen_ai.request.model"].endsWith("-mini")
```

### List membership
```cel
// Tool is in allowed list (whitelist approach — block everything NOT in list)
!(attrs["gen_ai.tool.name"] in ["search", "read_file", "summarize", "calculate"])

// Agent is in blocked list
attrs["gen_ai.agent.name"] in ["untrusted-bot", "test-agent"]

// Model is expensive
attrs["gen_ai.request.model"] in ["gpt-4o", "claude-opus-4", "gemini-1.5-pro"]
```

### Time-based rules
```cel
// Business hours only (UTC)
now.getHours() >= 9 && now.getHours() < 17

// Weekdays only
now.getDayOfWeek() >= 1 && now.getDayOfWeek() <= 5
```

---

## Common Policy Templates

These are available as one-click templates in the dashboard.

### Block prompt injection
```cel
attrs["gen_ai.content"].matches("(?i)(ignore previous|disregard|forget your|you are now|act as|pretend to be|system prompt)")
```
Action: **block**

### Block PII outbound
```cel
attrs["gen_ai.content"].matches("\\b\\d{3}-\\d{2}-\\d{4}\\b") || attrs["gen_ai.content"].matches("\\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Z|a-z]{2,}\\b")
```
Action: **block**: stops the call when the content carries an SSN or email
address. (Redaction itself is not a policy action: Strathon redacts PII from
stored spans automatically at ingest: see [redaction](https://getstrathon.com/docs/redaction).)

### Tool allowlist (production)
```cel
!(attrs["gen_ai.tool.name"] in ["search", "read_document", "summarize", "calculate", "send_response"])
```
Action: **block**: blocks anything NOT in the list

### Require approval for destructive actions
```cel
attrs["gen_ai.tool.name"] in ["delete", "drop_table", "send_email", "deploy", "shutdown"]
```
Action: **require_approval**

### Budget cap per model
```cel
attrs["gen_ai.request.model"] == "gpt-4o" && attrs["gen_ai.usage.cost"] > 0.10
```
Action: **throttle**

### Block secret leakage
```cel
attrs["gen_ai.content"].matches("(?i)(sk-[a-zA-Z0-9]{20,}|ghp_[a-zA-Z0-9]{36}|-----BEGIN.*PRIVATE KEY-----)")
```
Action: **block**

### Shadow mode (test without blocking)
Any policy can be set to **shadow mode** in the dashboard. Shadow policies evaluate every span but never enforce; results appear in traces for review. Use shadow mode to test a new policy before enabling enforcement.

---

## Attributes Reference

| Attribute | Type | Description |
|-----------|------|-------------|
| `attrs["gen_ai.tool.name"]` | string | Tool being called |
| `attrs["gen_ai.agent.name"]` | string | Agent name |
| `attrs["gen_ai.agent.id"]` | string | Agent unique ID |
| `attrs["gen_ai.content"]` | string | Prompt or response text |
| `attrs["gen_ai.request.model"]` | string | Model name |
| `attrs["gen_ai.usage.cost"]` | float | Cost in USD |
| `attrs["gen_ai.usage.input_tokens"]` | int | Input token count |
| `attrs["gen_ai.usage.output_tokens"]` | int | Output token count |
| `attrs["gen_ai.workflow.name"]` | string | Workflow name |
| `attrs["gen_ai.conversation.id"]` | string | Conversation ID |
| `now` | timestamp | Current UTC time |

---

## Don't know CEL? Use AI to generate it

Copy this prompt into Claude, ChatGPT, or any AI assistant:

```
You are a Strathon CEL policy generator. Convert my plain English
description into a CEL expression for the Strathon AI agent firewall.

Available attributes in every span:
- attrs["gen_ai.tool.name"]     — tool being called (string)
- attrs["strathon.tool.args"]   — tool input arguments, as a JSON string
- attrs["gen_ai.agent.name"]    — agent name (string)
- attrs["gen_ai.content"]       — prompt or response text (string)
- attrs["gen_ai.request.model"] — model name (string)
- attrs["gen_ai.usage.cost"]    — cost in USD (float)
- attrs["gen_ai.workflow.name"] — workflow name (string)
- now                           — current UTC timestamp

Available actions: block, steer, throttle, log, alert, require_approval, allow

CEL syntax basics:
- == for equality, != for not equal
- && for AND, || for OR, ! for NOT
- "in" for list membership: x in ["a", "b"]
- .matches("regex") for regex matching
- .startsWith("prefix"), .endsWith("suffix")
- .contains("substring")

Output only the CEL expression. No explanation.

My policy: [DESCRIBE WHAT YOU WANT HERE]
```

### Examples

Tell the AI: "Block shell commands from any agent"
```cel
attrs["gen_ai.tool.name"] in ["shell_exec", "bash", "exec", "system"]
```

Tell the AI: "Require human approval when research-bot wants to send emails"
```cel
attrs["gen_ai.agent.name"] == "research-bot" && attrs["gen_ai.tool.name"] == "send_email"
```

Tell the AI: "Alert when any agent spends more than $1 on a single call"
```cel
attrs["gen_ai.usage.cost"] > 1.0
```

### Attribute namespaces (which prefix to use)

Attribute names use one of two prefixes, and the prefix matters; a policy that
references an attribute the engine doesn't emit silently never matches:

- **`gen_ai.*`**: OpenTelemetry's standard GenAI attributes (tool name, agent
  name, content, model, cost, workflow). Use these for the common fields; they
  are the convention and what most examples use.
- **`strathon.*`**: Strathon's own additions that aren't in the OTel standard,
  most importantly **`strathon.tool.args`** (the tool's input arguments, as a
  JSON string).

The one easy mistake: the tool *name* is `gen_ai.tool.name`, but the tool
*arguments* are `strathon.tool.args`: different prefixes. There is no
`gen_ai.tool.args`. When in doubt, use the names exactly as listed in this
reference rather than guessing the prefix.

---

## Need Help?

- **Templates**: Use one-click policy templates in the dashboard, no CEL needed
- **AI Generation**: Copy the prompt above into Claude or ChatGPT
- **Documentation**: [getstrathon.com/docs](https://getstrathon.com/docs)
- **Community**: [Discord](https://discord.gg/Ta9XRmh4H)
- **Issues**: [GitHub Issues](https://github.com/strathon/strathon/issues)

# CLI Reference

`strathon-cli` is the command-line interface for Strathon. It talks to the same
receiver REST API the dashboard and SDK use, so anything you can do in the
dashboard you can script in CI.

## Install

```bash
pip install strathon-cli
```

## Configuration

The CLI reads two environment variables:

| Variable | Required | Default |
|----------|----------|---------|
| `STRATHON_API_KEY` | Yes | |
| `STRATHON_ENDPOINT` | No | `http://localhost:4318` |

```bash
export STRATHON_API_KEY=stra_...
export STRATHON_ENDPOINT=https://strathon.your-domain.com   # optional
```

Every command accepts a `--json` flag that prints raw JSON instead of formatted
tables, for piping into `jq` or consuming in scripts and CI pipelines.

## Command groups

The CLI is organized into 14 command groups. Run `strathon --help` or
`strathon <group> --help` for the full option list on any command.

### policies

Manage firewall policies.

```bash
strathon policies list
strathon policies create --name "block-email" \
  --expr 'attrs["gen_ai.tool.name"] == "send_email"' --action block
strathon policies create --template block-dangerous-tools
strathon policies create --from-english "block all shell commands"
strathon policies import policies.yaml
strathon policies test --name my-policy --last 100
strathon policies get <policy-id>
strathon policies enable <policy-id>
strathon policies disable <policy-id>
strathon policies delete <policy-id>
strathon policies suggest
strathon policies conflicts
```

`policies create` requires exactly one of `--expr`, `--template`, or
`--from-english`. The `--template` form creates a policy from a built-in
OWASP-mapped template without writing CEL; `--from-english` generates a CEL
expression from a plain-English description for you to review and confirm.
`policies import` accepts YAML or JSON. `policies test` dry-runs a policy
against recent traces without enforcing it. `policies suggest` proposes policies
from observed traffic, and `policies conflicts` flags contradictory rules.

### traces

```bash
strathon traces list --last 1h
strathon traces tree <trace-id>
```

### spans

```bash
strathon spans search --tool send_email --limit 50
```

### halts

Operator kill-switches. See [Runtime Intervention](intervention.md).

```bash
strathon halts list
strathon halts create --scope project --reason "Emergency"
strathon halts delete <halt-id>
```

### templates

```bash
strathon templates list
strathon templates apply <template-name>
```

### agents

```bash
strathon agents list
```

### compliance

```bash
strathon compliance export --format sarif
```

### budgets

Cost and iteration budgets. See [Budgets](budgets.md).

```bash
strathon budgets list
strathon budgets create --name "monthly cap" --type cost --limit 100 --window fixed
strathon budgets forecast
strathon budgets delete <budget-id>
```

### audit

```bash
strathon audit list --last 24h
```

### projects

```bash
strathon projects list
strathon projects create --name "Production Agents" --slug prod-agents
```

### approvals

Human-in-the-loop approvals. See [Human Approval](approvals.md).

```bash
strathon approvals list --status pending
strathon approvals approve <approval-id>
strathon approvals deny <approval-id>
```

### notifications

```bash
strathon notifications list
```

### keys

Create and manage API keys. See [API Keys](api_keys.md).

```bash
strathon keys list
strathon keys create --name "ci-agent" --scope traces:write
strathon keys rotate <key-id>
strathon keys revoke <key-id>
```

`keys create` prints the full key once and never again, so copy it immediately.
Omit `--scope` for the SDK default (`traces:write`, `policies:read`), or pass
`--scope '*'` for an admin key. `keys rotate` issues a new secret and invalidates
the old one.

### admin

```bash
strathon admin list-users
strathon admin create-user --email user@company.com
strathon admin reset-password --email user@company.com
strathon admin transfer-ownership --to user@company.com
strathon admin revoke-all-keys
```

## Scripting example

Because every command supports `--json`, the CLI composes with standard Unix
tooling. For example, list every policy currently in shadow status:

```bash
strathon policies list --json | jq '.[] | select(.shadow == true) | .name'
```

## See also

- [Runtime Intervention](intervention.md) — policies, actions, halts, budgets, webhooks
- [CEL Reference](cel-reference.md) — the policy match-expression language
- [API Keys](api_keys.md) — creating and scoping the keys the CLI authenticates with

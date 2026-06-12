# Strathon CLI

Command-line interface for [Strathon](https://github.com/strathon/strathon), the open-source AI agent firewall.

## Install

```bash
pip install strathon-cli
```

## Usage

```bash
# Set your API key
export STRATHON_API_KEY=stra_...

# Policy management
strathon policies list
strathon policies create --name "block-email" \
  --expr 'attrs["gen_ai.tool.name"] == "send_email"' --action block
strathon policies create --template block-prompt-injection
strathon policies create --from-english "block all shell commands"   # needs STRATHON_AI_API_KEY on the receiver
strathon policies import policies.yaml
strathon policies test --name my-policy --last 100

# Traces and spans
strathon traces list --limit 50 --agent my-agent
strathon traces tree <trace-id>
strathon spans search --tool send_email --limit 50

# Operations
strathon halts create --scope project --reason "Emergency"
strathon budgets list
strathon approvals list --status pending

# Compliance and audit
strathon compliance export --format sarif
strathon audit list --limit 100

# Administration
strathon admin list-users
strathon admin reset-password --email user@company.com
```

Every read command takes a `--json` flag for scripting and CI pipelines.

## Configuration

| Variable | Required | Default |
|----------|----------|---------|
| `STRATHON_API_KEY` | Yes | |
| `STRATHON_ENDPOINT` | No | `http://localhost:4318` |

## Documentation

- [CLI reference](https://getstrathon.com/docs/cli)
- [Policy engine](https://getstrathon.com/docs/intervention)
- [CEL reference](https://getstrathon.com/docs/cel-reference)
- [GitHub](https://github.com/strathon/strathon)

## License

Apache License 2.0. See [LICENSE](https://github.com/strathon/strathon/blob/main/cli/LICENSE).

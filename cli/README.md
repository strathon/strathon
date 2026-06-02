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
strathon policies create --from-english "block all shell commands"
strathon policies import policies.yaml
strathon policies test --name my-policy --last 100

# Traces and spans
strathon traces list --last 1h
strathon traces tree <trace-id>
strathon spans search --tool send_email --limit 50

# Operations
strathon halts create --scope project --reason "Emergency"
strathon budgets list
strathon approvals list --pending

# Compliance and audit
strathon compliance export --format sarif
strathon audit list --last 24h

# Administration
strathon admin list-users
strathon admin reset-password --email user@company.com
```

Every command supports `--json` for scripting and CI pipelines.

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

MIT. See [LICENSE](https://github.com/strathon/strathon/blob/main/LICENSE).

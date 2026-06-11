"""Strathon CLI — command-line interface for the AI agent firewall.

Usage:
    strathon policies list
    strathon policies create --name "block-email" --expr ... --action block
    strathon policies delete <id>
    strathon traces list
    strathon spans search --q "send_email"
    strathon halts create --scope project
    strathon templates list
    strathon templates apply <template-id>

Environment variables:
    STRATHON_API_KEY       Required. API key (stra_...).
    STRATHON_ENDPOINT      Receiver URL (default: http://localhost:4318).

Research: Click 8.x CLI patterns, Rich table formatting for terminal
output, standard CLI UX conventions (--json for machine-readable,
tables for human-readable).
"""

from __future__ import annotations

import json as json_mod

import click
from rich.console import Console
from rich.table import Table

from . import __version__
from .client import api_delete, api_get, api_patch, api_post

console = Console()


# ---- Root group --------------------------------------------------------------

@click.group()
@click.version_option(__version__, prog_name="strathon")
def cli():
    """Strathon — the open-source AI agent firewall.

    Manage policies, traces, halts, and templates from the command line.
    Set STRATHON_API_KEY and optionally STRATHON_ENDPOINT before use.
    """


# ---- Policies ----------------------------------------------------------------

@cli.group()
def policies():
    """Manage firewall policies."""


@policies.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def policies_list(as_json: bool):
    """List all policies."""
    data = api_get("/v1/policies")
    items = data if isinstance(data, list) else data.get("data", data)

    if as_json:
        click.echo(json_mod.dumps(items, indent=2))
        return

    if not items:
        click.echo("No policies found.")
        return

    table = Table(title="Policies")
    table.add_column("ID", style="dim", max_width=12)
    table.add_column("Name", style="bold")
    table.add_column("Action", style="cyan")
    table.add_column("Enabled")
    table.add_column("Shadow")
    table.add_column("Priority")

    for p in items:
        pid = str(p.get("id", ""))[:12]
        enabled = "[green]yes[/]" if p.get("enabled") else "[red]no[/]"
        shadow = "[yellow]shadow[/]" if p.get("shadow") else "-"
        table.add_row(
            pid, p.get("name", ""), p.get("action", ""),
            enabled, shadow, str(p.get("priority", 0)),
        )
    console.print(table)


@policies.command("create")
@click.option("--name", default=None, help="Policy name")
@click.option("--expr", default=None, help="CEL match expression")
@click.option("--template", default=None,
              help="Create from a built-in template (e.g. block-prompt-injection)")
@click.option("--from-english", "from_english", default=None,
              help="Describe policy in plain English")
@click.option("--action", default=None,
              type=click.Choice(
                  ["block", "steer", "throttle",
                   "log", "alert", "require_approval", "allow"]),
              help="Enforcement action")
@click.option("--shadow", is_flag=True, help="Create as shadow policy")
@click.option("--priority", default=0, help="Priority (higher = first)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def policies_create(name, expr, template, from_english, action, shadow, priority, as_json):
    """Create a new policy.

    Three modes:

    \b
      --expr          Provide a CEL expression directly
      --template      Create from a built-in template by name
      --from-english  Describe the policy in plain English

    With --template, --name and --action are optional (the template provides defaults).
    With --from-english, the generated CEL is shown for confirmation before creating.
    """
    modes = sum(1 for x in (expr, template, from_english) if x is not None)
    if modes == 0:
        raise click.UsageError("Provide one of: --expr, --template, or --from-english")
    if modes > 1:
        raise click.UsageError("Only one of --expr, --template, or --from-english can be used")

    if template:
        # Fetch template from the receiver and create from it.
        templates_resp = api_get("/v1/policy-templates")
        templates_list = templates_resp.get("data", [])
        match = None
        for t in templates_list:
            slug = t.get("slug") or t.get("name", "").lower().replace(" ", "-")
            if slug == template or t.get("name", "").lower() == template.lower():
                match = t
                break
        if not match:
            available = [t.get("slug") or t.get("name", "").lower().replace(" ", "-")
                         for t in templates_list]
            click.echo(f"Template '{template}' not found.", err=True)
            if available:
                click.echo(f"Available: {', '.join(available)}", err=True)
            raise SystemExit(1)

        body = {
            "name": name or match.get("name", template),
            "match_expression": match.get("match_expression", ""),
            "action": action or match.get("action", "block"),
            "shadow": shadow,
            "priority": priority,
        }
        result = api_post("/v1/policies", json=body)
        if as_json:
            click.echo(json_mod.dumps(result, indent=2))
        else:
            click.echo(f"Created policy {result.get('id', '')} from template '{template}'")
        return

    if from_english:
        # Ask the receiver to generate CEL from English description.
        try:
            gen_result = api_post("/v1/policies/generate", json={"description": from_english})
        except (SystemExit, Exception):
            click.echo("AI policy generation failed.", err=True)
            click.echo("Ensure STRATHON_AI_API_KEY is set on the receiver.", err=True)
            click.echo("Manual reference: getstrathon.com/docs/cel-reference", err=True)
            raise SystemExit(1)
        generated_expr = gen_result.get("match_expression", "")
        generated_action = gen_result.get("action", "block")
        generated_name = gen_result.get("name", from_english[:50])

        console.print(f"\n  [bold]Description:[/] {from_english}")
        console.print(f"  [bold]Generated CEL:[/] {generated_expr}")
        console.print(f"  [bold]Action:[/] {action or generated_action}")
        console.print()

        if not click.confirm("  Create this policy?"):
            click.echo("Aborted.")
            return

        body = {
            "name": name or generated_name,
            "match_expression": generated_expr,
            "action": action or generated_action,
            "shadow": shadow,
            "priority": priority,
        }
        result = api_post("/v1/policies", json=body)
        if as_json:
            click.echo(json_mod.dumps(result, indent=2))
        else:
            click.echo(f"Created policy {result.get('id', '')} ({body['name']})")
        return

    # Direct --expr mode (original behavior).
    if not name:
        raise click.UsageError("--name is required when using --expr")
    if not action:
        raise click.UsageError("--action is required when using --expr")

    body = {
        "name": name,
        "match_expression": expr,
        "action": action,
        "shadow": shadow,
        "priority": priority,
    }
    result = api_post("/v1/policies", json=body)

    if as_json:
        click.echo(json_mod.dumps(result, indent=2))
    else:
        click.echo(f"Created policy {result.get('id', '')} ({name})")


@policies.command("import")
@click.argument("filepath", type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Validate without creating")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def policies_import(filepath, dry_run, as_json):
    """Bulk import policies from a YAML or JSON file.

    \b
    Expected format (YAML):
      policies:
        - name: block-email
          match_expression: 'attrs["gen_ai.tool.name"] == "send_email"'
          action: block
        - name: log-shell
          match_expression: 'attrs["gen_ai.tool.name"] == "run_shell"'
          action: log
    """
    import yaml  # noqa: E402 — lazy import, yaml is optional dep

    with open(filepath) as f:
        if filepath.endswith((".yaml", ".yml")):
            data = yaml.safe_load(f)
        else:
            data = json_mod.load(f)

    items = data.get("policies", [])
    if not items:
        click.echo("No policies found in file.", err=True)
        raise SystemExit(1)

    results = []
    for i, p in enumerate(items):
        p_name = p.get("name", f"imported-{i}")
        p_expr = p.get("match_expression", "")
        p_action = p.get("action", "block")
        if not p_expr:
            click.echo(f"Skipping '{p_name}': no match_expression", err=True)
            continue

        if dry_run:
            results.append({"name": p_name, "status": "valid"})
            continue

        body = {
            "name": p_name,
            "match_expression": p_expr,
            "action": p_action,
            "shadow": p.get("shadow", False),
            "priority": p.get("priority", 0),
        }
        result = api_post("/v1/policies", json=body)
        results.append(result)

    if as_json:
        click.echo(json_mod.dumps(results, indent=2))
    else:
        verb = "validated" if dry_run else "imported"
        click.echo(f"{verb} {len(results)} policies from {filepath}")


@policies.command("test")
@click.option("--name", required=True, help="Policy name to test")
@click.option("--last", "last_n", default=100, type=int,
              help="Number of recent traces to test against (default: 100)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def policies_test(name, last_n, as_json):
    """Test a policy against recent traces (dry run).

    Fetches the last N traces and shows which spans would have matched
    the named policy. Useful for validating a policy before enabling it.
    """
    # Get the policy by name.
    policies_resp = api_get("/v1/policies")
    policy_list = policies_resp.get("policies", [])
    policy = None
    for p in policy_list:
        if p.get("name") == name:
            policy = p
            break
    if not policy:
        click.echo(f"Policy '{name}' not found.", err=True)
        raise SystemExit(1)

    # Fetch recent traces.
    traces_resp = api_get("/v1/traces", params={"limit": last_n})
    traces = traces_resp.get("traces", [])

    if not traces:
        click.echo("No traces found to test against.")
        return

    # Ask the receiver to evaluate the policy against these traces.
    test_result = api_post("/v1/policies/simulate", json={
        "match_expression": policy.get("match_expression", ""),
        "hours": 24,
    })

    matches = test_result.get("matches", [])

    if as_json:
        click.echo(json_mod.dumps(test_result, indent=2))
        return

    click.echo(f"Policy: {name} ({policy.get('action', 'block')})")
    click.echo(f"Tested against: {test_result.get('traces_tested', len(traces))} traces, "
               f"{test_result.get('spans_tested', 0)} spans")
    click.echo(f"Matches: {len(matches)}")
    click.echo()

    if matches:
        table = Table(title="Matched Spans")
        table.add_column("Trace ID", style="dim")
        table.add_column("Span Name")
        table.add_column("Tool")
        table.add_column("Timestamp")
        for m in matches[:20]:
            table.add_row(
                m.get("trace_id", "")[:12],
                m.get("span_name", ""),
                m.get("tool_name", ""),
                m.get("timestamp", ""),
            )
        console.print(table)
        if len(matches) > 20:
            click.echo(f"  ... and {len(matches) - 20} more (use --json for full list)")


@policies.command("get")
@click.argument("policy_id")
@click.option("--json", "as_json", is_flag=True)
def policies_get(policy_id, as_json):
    """Get a policy by ID."""
    result = api_get(f"/v1/policies/{policy_id}")

    if as_json:
        click.echo(json_mod.dumps(result, indent=2))
        return

    for k, v in result.items():
        click.echo(f"  {k}: {v}")


@policies.command("delete")
@click.argument("policy_id")
@click.confirmation_option(prompt="Delete this policy?")
def policies_delete(policy_id):
    """Delete a policy."""
    api_delete(f"/v1/policies/{policy_id}")
    click.echo(f"Deleted policy {policy_id}")


@policies.command("enable")
@click.argument("policy_id")
def policies_enable(policy_id):
    """Enable a policy."""
    api_patch(f"/v1/policies/{policy_id}", json={"enabled": True})
    click.echo(f"Enabled policy {policy_id}")


@policies.command("disable")
@click.argument("policy_id")
def policies_disable(policy_id):
    """Disable a policy."""
    api_patch(f"/v1/policies/{policy_id}", json={"enabled": False})
    click.echo(f"Disabled policy {policy_id}")


@policies.command("suggest")
@click.option("--json", "as_json", is_flag=True)
def policies_suggest(as_json):
    """Get automated policy suggestions based on recent span data."""
    result = api_get("/v1/policies/suggest")
    suggestions = result.get("suggestions", [])

    if as_json:
        click.echo(json_mod.dumps(result, indent=2))
        return

    if not suggestions:
        click.echo("No suggestions — your policies look good.")
        return

    for s in suggestions:
        risk = s.get("risk_level", "medium")
        color = {"high": "red", "medium": "yellow", "low": "green"}.get(risk, "white")
        console.print(f"  [{color}]{risk.upper()}[/] {s.get('reason', '')}")
        if s.get("owasp_ref"):
            console.print(f"        OWASP: {s['owasp_ref']}")
        click.echo()


@policies.command("conflicts")
@click.option("--json", "as_json", is_flag=True)
def policies_conflicts(as_json):
    """Detect policy conflicts."""
    result = api_get("/v1/policies/conflicts")

    if as_json:
        click.echo(json_mod.dumps(result, indent=2))
        return

    conflicts = result.get("conflicts", [])
    if not conflicts:
        click.echo(f"No conflicts detected ({result.get('policies_analyzed', 0)} policies analyzed).")
        return

    for c in conflicts:
        ctype = c.get("type", "unknown")
        color = "red" if ctype == "contradiction" else "yellow"
        console.print(f"  [{color}]{ctype}[/]: {c.get('reason', '')}")
        console.print(f"    Policy A: {c['policy_a'].get('name', '')}")
        console.print(f"    Policy B: {c['policy_b'].get('name', '')}")
        click.echo()


# ---- Traces ------------------------------------------------------------------

@cli.group()
def traces():
    """Query traces."""


@traces.command("list")
@click.option("--limit", default=20, help="Number of traces")
@click.option("--agent", default=None, help="Filter by agent name")
@click.option("--json", "as_json", is_flag=True)
def traces_list(limit, agent, as_json):
    """List recent traces."""
    params = {"limit": limit}
    if agent:
        params["agent_name"] = agent

    result = api_get("/v1/traces", params=params)
    items = result.get("data", [])

    if as_json:
        click.echo(json_mod.dumps(result, indent=2))
        return

    if not items:
        click.echo("No traces found.")
        return

    table = Table(title="Traces")
    table.add_column("Trace ID", style="dim", max_width=16)
    table.add_column("Agent")
    table.add_column("Spans", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Intervention")

    for t in items:
        tid = str(t.get("trace_id", ""))[:16]
        cost = t.get("total_cost_usd", "-")
        state = t.get("intervention_state") or "-"
        color = "red" if state == "blocked" else "green" if state == "allowed" else "white"
        table.add_row(
            tid, t.get("agent_name", "-"),
            str(t.get("span_count", "-")),
            str(cost),
            f"[{color}]{state}[/]",
        )
    console.print(table)


@traces.command("tree")
@click.argument("trace_id")
@click.option("--json", "as_json", is_flag=True)
def traces_tree(trace_id, as_json):
    """Show the span tree for a trace."""
    result = api_get(f"/v1/traces/{trace_id}/tree")

    if as_json:
        click.echo(json_mod.dumps(result, indent=2))
        return

    click.echo(f"Trace: {trace_id}")
    click.echo(f"Spans: {result.get('span_count', '?')}")
    root = result.get("root")
    if root:
        _print_tree(root, indent=0)


def _print_tree(node: dict, indent: int):
    """Recursively print a span tree."""
    prefix = "  " * indent + ("├─ " if indent > 0 else "")
    name = node.get("name") or node.get("operation_name") or "?"
    tool = node.get("tool_name")
    state = node.get("intervention_state") or ""
    suffix = f" [{state}]" if state else ""
    if tool:
        suffix = f" → {tool}{suffix}"
    click.echo(f"{prefix}{name}{suffix}")
    for child in node.get("children", []):
        _print_tree(child, indent + 1)


# ---- Spans -------------------------------------------------------------------

@cli.group()
def spans():
    """Search and query spans."""


@spans.command("search")
@click.option("--q", "query", default=None, help="Full-text search query")
@click.option("--agent", default=None, help="Filter by agent name")
@click.option("--tool", default=None, help="Filter by tool name")
@click.option("--model", default=None, help="Filter by model")
@click.option("--limit", default=20)
@click.option("--json", "as_json", is_flag=True)
def spans_search(query, agent, tool, model, limit, as_json):
    """Search spans with full-text search and filters."""
    params = {"limit": limit}
    if query:
        params["q"] = query
    if agent:
        params["agent_name"] = agent
    if tool:
        params["tool_name"] = tool
    if model:
        params["request_model"] = model

    result = api_get("/v1/spans", params=params)
    items = result.get("data", result) if isinstance(result, dict) else result

    if as_json:
        click.echo(json_mod.dumps(items, indent=2))
        return

    if not items:
        click.echo("No spans found.")
        return

    table = Table(title="Spans")
    table.add_column("Span ID", style="dim", max_width=12)
    table.add_column("Name")
    table.add_column("Agent")
    table.add_column("Tool")
    table.add_column("Model")
    table.add_column("Cost", justify="right")

    for s in items if isinstance(items, list) else []:
        sid = str(s.get("span_id", ""))[:12]
        table.add_row(
            sid,
            s.get("name", "-"),
            s.get("agent_name", "-"),
            s.get("tool_name", "-"),
            s.get("request_model", "-"),
            str(s.get("cost_usd", "-")),
        )
    console.print(table)


# ---- Halts -------------------------------------------------------------------

@cli.group()
def halts():
    """Manage operator kill-switches."""


@halts.command("list")
@click.option("--json", "as_json", is_flag=True)
def halts_list(as_json):
    """List active halts."""
    result = api_get("/v1/halts")
    items = result.get("data", []) if isinstance(result, dict) else result

    if as_json:
        click.echo(json_mod.dumps(items, indent=2))
        return

    if not items:
        click.echo("No active halts.")
        return

    table = Table(title="Halts")
    table.add_column("ID", style="dim", max_width=12)
    table.add_column("Scope")
    table.add_column("Agent")
    table.add_column("Reason")
    table.add_column("Created")

    for h in items if isinstance(items, list) else []:
        table.add_row(
            str(h.get("id", ""))[:12],
            h.get("scope", "-"),
            h.get("agent_name") or "all",
            h.get("reason", "-"),
            str(h.get("created_at", ""))[:19],
        )
    console.print(table)


@halts.command("create")
@click.option("--scope", required=True,
              type=click.Choice(["project", "agent"]),
              help="Halt scope")
@click.option("--agent", default=None, help="Agent name (required for agent scope)")
@click.option("--reason", default="CLI halt", help="Reason for halt")
def halts_create(scope, agent, reason):
    """Create a halt (kill-switch)."""
    body = {"scope": scope, "reason": reason}
    if agent:
        body["agent_name"] = agent

    result = api_post("/v1/halts", json=body)
    click.echo(f"Halt created: {result.get('id', '')}")


@halts.command("delete")
@click.argument("halt_id")
@click.confirmation_option(prompt="Remove this halt?")
def halts_delete(halt_id):
    """Remove a halt."""
    api_delete(f"/v1/halts/{halt_id}")
    click.echo(f"Halt removed: {halt_id}")


# ---- Templates ---------------------------------------------------------------

@cli.group()
def templates():
    """Browse and apply OWASP-mapped policy templates."""


@templates.command("list")
@click.option("--json", "as_json", is_flag=True)
def templates_list(as_json):
    """List available policy templates."""
    result = api_get("/v1/policy-templates")
    items = result.get("data", []) if isinstance(result, dict) else result

    if as_json:
        click.echo(json_mod.dumps(items, indent=2))
        return

    if not items:
        click.echo("No templates available.")
        return

    table = Table(title="Policy Templates")
    table.add_column("ID", style="dim")
    table.add_column("Name", style="bold")
    table.add_column("Action", style="cyan")
    table.add_column("OWASP Risks")
    table.add_column("Description", max_width=40)

    for t in items if isinstance(items, list) else []:
        risks = ", ".join(t.get("owasp_risks", []))
        table.add_row(
            t.get("id", ""),
            t.get("name", ""),
            t.get("action", ""),
            risks,
            (t.get("description", ""))[:40],
        )
    console.print(table)


@templates.command("apply")
@click.argument("template_id")
def templates_apply(template_id):
    """Apply a template to create a policy."""
    result = api_post(f"/v1/policy-templates/{template_id}/apply")
    policy = result.get("policy", {})
    click.echo(f"Applied template {template_id}")
    click.echo(f"  Policy created: {policy.get('id', '')} ({policy.get('name', '')})")


# ---- Agents ------------------------------------------------------------------

@cli.group()
def agents():
    """View agent inventory and risk scores."""


@agents.command("list")
@click.option("--json", "as_json", is_flag=True)
def agents_list(as_json):
    """List discovered agents with risk scores."""
    result = api_get("/v1/agents")
    items = result.get("data", []) if isinstance(result, dict) else result

    if as_json:
        click.echo(json_mod.dumps(items, indent=2))
        return

    if not items:
        click.echo("No agents discovered yet.")
        return

    table = Table(title="Agent Inventory")
    table.add_column("Agent", style="bold")
    table.add_column("Risk", justify="center")
    table.add_column("Tools", justify="right")
    table.add_column("Policies", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Last Active")

    for a in items if isinstance(items, list) else []:
        risk = a.get("risk_score", "?")
        color = {"high": "red", "medium": "yellow", "low": "green"}.get(risk, "white")
        table.add_row(
            a.get("agent_name", "?"),
            f"[{color}]{risk.upper()}[/]",
            str(a.get("total_tool_calls", "-")),
            str(a.get("policies_covering", "-")),
            str(a.get("total_cost_usd", "-")),
            str(a.get("last_active", ""))[:19],
        )
    console.print(table)


# ---- Compliance --------------------------------------------------------------

@cli.group()
def compliance():
    """EU AI Act compliance tools."""


@compliance.command("export")
@click.option("--format", "fmt", type=click.Choice(["json", "sarif"]), default="json",
              help="Output format. 'sarif' emits a SARIF 2.1.0 log.")
@click.option("--output", "-o", "output", type=click.Path(), default=None,
              help="Write the package to a file instead of stdout.")
@click.option("--json", "as_json", is_flag=True,
              help="Print the raw JSON package (alias for --format json -o -).")
def compliance_export(fmt, output, as_json):
    """Generate EU AI Act compliance evidence package."""
    if as_json:
        fmt = "json"
    result = api_post("/v1/compliance/export", json={"format": fmt})

    # SARIF (or explicit JSON to a file / stdout): emit the document verbatim.
    if fmt == "sarif" or output or as_json:
        text = json_mod.dumps(result, indent=2)
        if output:
            with open(output, "w", encoding="utf-8") as fh:
                fh.write(text)
            console.print(f"[green]Wrote {fmt.upper()} package to {output}[/]")
        else:
            click.echo(text)
        return

    recs = result.get("recommendations", [])
    articles = result.get("articles", {})

    click.echo("EU AI Act Compliance Export")
    click.echo("=" * 40)

    for art_key, art_data in articles.items():
        compliant = art_data.get("compliant", False)
        status = "[green]COMPLIANT[/]" if compliant else "[red]NON-COMPLIANT[/]"
        console.print(f"  {art_key}: {status}")

    if recs:
        click.echo()
        console.print("[yellow]Recommendations:[/]")
        for r in recs:
            console.print(f"  - {r}")
    else:
        click.echo()
        console.print("[green]All checks passed.[/]")


# ---- Budgets -----------------------------------------------------------------

@cli.group()
def budgets():
    """Manage cost and iteration budgets."""


@budgets.command("list")
@click.option("--json", "as_json", is_flag=True)
def budgets_list(as_json):
    """List all budgets."""
    result = api_get("/v1/budgets")
    items = result.get("data", []) if isinstance(result, dict) else result

    if as_json:
        click.echo(json_mod.dumps(items, indent=2))
        return

    if not items:
        click.echo("No budgets configured.")
        return

    table = Table(title="Budgets")
    table.add_column("ID", style="dim", max_width=12)
    table.add_column("Name", style="bold")
    table.add_column("Type")
    table.add_column("Limit", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("Status")

    for b in items if isinstance(items, list) else []:
        current = b.get("current_spend") or b.get("current_count", "-")
        limit_val = b.get("limit_usd") or b.get("limit_count", "-")
        halted = b.get("auto_halted", False)
        status = "[red]HALTED[/]" if halted else "[green]active[/]"
        table.add_row(
            str(b.get("id", ""))[:12],
            b.get("name", "-"),
            b.get("budget_type", "-"),
            str(limit_val),
            str(current),
            status,
        )
    console.print(table)


@budgets.command("create")
@click.option("--name", required=True)
@click.option("--type", "budget_type", required=True,
              type=click.Choice(["cost", "iteration"]))
@click.option("--limit", required=True, type=float, help="USD limit or iteration count")
@click.option("--window", default="fixed", type=click.Choice(["fixed", "rolling"]))
def budgets_create(name, budget_type, limit, window):
    """Create a budget."""
    body = {"name": name, "budget_type": budget_type, "window_type": window}
    if budget_type == "cost":
        body["limit_usd"] = limit
    else:
        body["limit_count"] = int(limit)

    result = api_post("/v1/budgets", json=body)
    click.echo(f"Budget created: {result.get('id', '')}")


@budgets.command("delete")
@click.argument("budget_id")
@click.confirmation_option(prompt="Delete this budget?")
def budgets_delete(budget_id):
    """Delete a budget."""
    api_delete(f"/v1/budgets/{budget_id}")
    click.echo(f"Budget deleted: {budget_id}")


@budgets.command("forecast")
@click.option("--json", "as_json", is_flag=True)
def budgets_forecast(as_json):
    """Show cost forecast and burn rate."""
    result = api_get("/v1/costs/forecast")

    if as_json:
        click.echo(json_mod.dumps(result, indent=2))
        return

    burn = result.get("burn_rate_usd_per_hour", 0)
    daily = result.get("projected_daily_cost", 0)
    weekly = result.get("projected_weekly_cost", 0)

    click.echo("Cost Forecast")
    click.echo(f"  Burn rate:  ${burn}/hour")
    click.echo(f"  Daily:      ${daily}")
    click.echo(f"  Weekly:     ${weekly}")

    budget_alerts = result.get("budget_alerts", [])
    if budget_alerts:
        click.echo()
        console.print("[yellow]Budget Alerts:[/]")
        for a in budget_alerts:
            console.print(f"  - {a.get('budget_name', '?')}: exhausted by {a.get('projected_exhaustion_date', '?')}")


# ---- Audit -------------------------------------------------------------------

@cli.group()
def audit():
    """Query the tamper-evident audit log."""


@audit.command("list")
@click.option("--limit", default=20)
@click.option("--action", "action_filter", default=None, help="Filter by action type")
@click.option("--json", "as_json", is_flag=True)
def audit_list(limit, action_filter, as_json):
    """List recent audit events."""
    params = {"limit": limit}
    if action_filter:
        params["filter"] = f'action eq "{action_filter}"'

    result = api_get("/v1/audit", params=params)
    items = result.get("data", []) if isinstance(result, dict) else result

    if as_json:
        click.echo(json_mod.dumps(items, indent=2))
        return

    if not items:
        click.echo("No audit events found.")
        return

    table = Table(title="Audit Log")
    table.add_column("Time", style="dim", max_width=19)
    table.add_column("Action", style="cyan")
    table.add_column("Category")
    table.add_column("Actor")
    table.add_column("Resource")

    for e in items if isinstance(items, list) else []:
        ts = str(e.get("created_at", ""))[:19]
        actor = e.get("actor_ip") or e.get("actor_key_prefix") or "-"
        resource = e.get("resource_type", "-")
        table.add_row(
            ts, e.get("action", "-"), e.get("category", "-"),
            actor, resource,
        )
    console.print(table)


# ---- Projects ----------------------------------------------------------------

@cli.group()
def projects():
    """Manage projects."""


@projects.command("list")
@click.option("--json", "as_json", is_flag=True)
def projects_list(as_json):
    """List all projects."""
    result = api_get("/v1/projects")
    items = result.get("data", []) if isinstance(result, dict) else result

    if as_json:
        click.echo(json_mod.dumps(items, indent=2))
        return

    if not items:
        click.echo("No projects found.")
        return

    table = Table(title="Projects")
    table.add_column("Slug", style="bold")
    table.add_column("Name")
    table.add_column("ID", style="dim", max_width=12)
    table.add_column("Created")

    for p in items if isinstance(items, list) else []:
        table.add_row(
            p.get("slug", "-"),
            p.get("name", "-"),
            str(p.get("id", ""))[:12],
            str(p.get("created_at", ""))[:19],
        )
    console.print(table)


@projects.command("create")
@click.option("--name", required=True)
@click.option("--slug", required=True)
def projects_create(name, slug):
    """Create a new project."""
    result = api_post("/v1/projects", json={"name": name, "slug": slug})
    click.echo(f"Project created: {result.get('slug', '')} ({result.get('id', '')})")
    if result.get("api_key"):
        click.echo(f"  API key: {result['api_key']}")


# ---- Approvals ---------------------------------------------------------------

@cli.group()
def approvals():
    """Manage human approval requests."""


@approvals.command("list")
@click.option("--status", "status_filter", default="pending",
              type=click.Choice(["pending", "approved", "denied", "expired"]))
@click.option("--json", "as_json", is_flag=True)
def approvals_list(status_filter, as_json):
    """List approval requests."""
    result = api_get("/v1/approvals", params={"status": status_filter})
    items = result.get("data", []) if isinstance(result, dict) else result

    if as_json:
        click.echo(json_mod.dumps(items, indent=2))
        return

    if not items:
        click.echo(f"No {status_filter} approvals.")
        return

    table = Table(title=f"Approvals ({status_filter})")
    table.add_column("ID", style="dim", max_width=12)
    table.add_column("Status")
    table.add_column("Policy")
    table.add_column("Requested", max_width=19)
    table.add_column("Expires", max_width=19)

    for a in items if isinstance(items, list) else []:
        status_val = a.get("status", "-")
        color = {
            "pending": "yellow", "approved": "green",
            "denied": "red", "expired": "dim",
        }.get(status_val, "white")
        table.add_row(
            str(a.get("id", ""))[:12],
            f"[{color}]{status_val}[/]",
            str(a.get("policy_id", "-"))[:12],
            str(a.get("requested_at", ""))[:19],
            str(a.get("expires_at", ""))[:19],
        )
    console.print(table)


@approvals.command("approve")
@click.argument("approval_id")
def approvals_approve(approval_id):
    """Approve a pending request."""
    api_post(f"/v1/approvals/{approval_id}/approve")
    click.echo(f"Approved: {approval_id}")


@approvals.command("deny")
@click.argument("approval_id")
def approvals_deny(approval_id):
    """Deny a pending request."""
    api_post(f"/v1/approvals/{approval_id}/deny")
    click.echo(f"Denied: {approval_id}")


# ---- Notifications -----------------------------------------------------------

@cli.group()
def notifications():
    """Manage notification channels (Slack, Discord, GitHub, webhook)."""


@notifications.command("list")
@click.option("--json", "as_json", is_flag=True)
def notifications_list(as_json):
    """List notification channels."""
    result = api_get("/v1/notification-channels")
    items = result.get("data", []) if isinstance(result, dict) else result

    if as_json:
        click.echo(json_mod.dumps(items, indent=2))
        return

    if not items:
        click.echo("No notification channels configured.")
        return

    table = Table(title="Notification Channels")
    table.add_column("ID", style="dim", max_width=12)
    table.add_column("Name", style="bold")
    table.add_column("Type", style="cyan")
    table.add_column("Enabled")
    table.add_column("Events")

    for ch in items if isinstance(items, list) else []:
        enabled = "[green]yes[/]" if ch.get("enabled") else "[red]no[/]"
        events = ", ".join(ch.get("events", [])) or "all"
        table.add_row(
            str(ch.get("id", ""))[:12],
            ch.get("name", "-"),
            ch.get("channel_type", "-"),
            enabled,
            events[:30],
        )
    console.print(table)


# ---- Admin commands ----------------------------------------------------------

@cli.group()
def admin():
    """Administrative commands for self-hosted deployments."""
    pass


@admin.command("reset-password")
@click.option("--email", required=True, help="User email")
def admin_reset_password(email):
    """Reset a user's password. Prints temporary password."""
    data = api_post( "/v1/auth/admin-reset-password", json={"email": email})
    if data:
        temp = data.get("temporary_password", "")
        console.print(f"[green]Password reset for {email}[/]")
        console.print(f"[bold]Temporary password: {temp}[/]")
        console.print("[yellow]User must change password on next login.[/]")


@admin.command("create-user")
@click.option("--email", required=True)
@click.option("--password", required=True)
@click.option("--display-name", default=None)
@click.option("--role", default="member", type=click.Choice(["owner", "admin", "operator", "viewer"]))
def admin_create_user(email, password, display_name, role):
    """Create a new user account."""
    data = api_post( "/v1/auth/register", json={
        "email": email,
        "password": password,
        "display_name": display_name or email.split("@")[0],
    })
    if data:
        console.print(f"[green]User {email} created ({role})[/]")


@admin.command("list-users")
def admin_list_users():
    """List all members of the current project."""
    data = api_get( "/v1/members")
    items = data.get("data", []) if data else []
    if not items:
        console.print("[yellow]No members found.[/]")
        return

    table = Table(title="Project Members")
    table.add_column("Email", style="bold")
    table.add_column("Display Name")
    table.add_column("Role", style="cyan")
    table.add_column("MFA")
    table.add_column("Last Active", style="dim")

    for m in items:
        mfa = "[green]yes[/]" if m.get("mfa_enabled") else "[red]no[/]"
        table.add_row(
            m.get("email", "-"),
            m.get("display_name", "-"),
            m.get("role", "-"),
            mfa,
            m.get("last_active", "-"),
        )
    console.print(table)


@admin.command("transfer-ownership")
@click.option("--to", "to_member", required=True, help="Member ID to transfer ownership to")
def admin_transfer_ownership(to_member):
    """Transfer project ownership to another admin."""
    data = api_post( f"/v1/members/{to_member}/transfer-ownership")
    if data:
        console.print("[green]Ownership transferred.[/]")


@admin.command("revoke-all-keys")
@click.confirmation_option(prompt="This will revoke ALL API keys. Continue?")
def admin_revoke_all_keys():
    """Revoke all API keys for the current project."""
    data = api_get( "/v1/api_keys")
    keys = data.get("data", []) if data else []
    for key in keys:
        api_delete( f"/v1/api_keys/{key['id']}")
    console.print(f"[green]Revoked {len(keys)} API keys.[/]")

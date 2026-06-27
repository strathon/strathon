"""Pre-built policy templates for common OWASP Agentic Security threats.

Each template maps to one or more ASI risks from the OWASP Top 10 for
Agentic Applications (2026). Operators can browse the catalog and apply
a template with one API call instead of writing CEL from scratch.

Templates are a static catalog — no DB table. Applying a template
creates a real policy via the standard create_policy path.

12 templates covering: tool misuse, data exfiltration, cost controls,
business hours, SQL injection, prompt injection, data protection, and
memory-poisoning attacks (MINJA-class, arXiv:2503.03704).

Research: OWASP Agentic Top 10 (ASI-01 through ASI-10), MINJA memory
injection attacks (arXiv:2503.03704).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PolicyTemplate:
    id: str
    name: str
    description: str
    owasp_risks: list[str]
    action: str
    match_expression: str
    action_config: dict[str, Any] = field(default_factory=dict)
    applies_to: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


TEMPLATES: list[PolicyTemplate] = [
    PolicyTemplate(
        id="block-dangerous-tools",
        name="Block dangerous tool names",
        description=(
            "Blocks tool calls to shell_exec, eval, rm, os.system, subprocess, "
            "and other tools that could execute arbitrary code or destructive "
            "commands. Covers ASI-02 (Tool Misuse) and ASI-05 (Unexpected Code Execution)."
        ),
        owasp_risks=["ASI-02", "ASI-05"],
        action="block",
        match_expression=(
            'attrs["gen_ai.tool.name"] in '
            '["shell_exec", "eval", "exec", "os_system", "subprocess_run", '
            '"rm", "rmdir", "drop_table", "delete_database", "format_disk"]'
        ),
        tags=["security", "tool-misuse", "code-execution"],
    ),
    PolicyTemplate(
        id="block-data-exfiltration",
        name="Block data exfiltration via HTTP tools",
        description=(
            "Blocks tool calls where arguments contain URLs pointing to "
            "external domains, preventing agents from sending internal data "
            "to attacker-controlled endpoints. Covers ASI-02 (Tool Misuse)."
        ),
        owasp_risks=["ASI-02"],
        action="block",
        match_expression=(
            '(attrs["gen_ai.tool.name"] == "http_request" || '
            'attrs["gen_ai.tool.name"] == "fetch" || '
            'attrs["gen_ai.tool.name"] == "web_request" || '
            'attrs["gen_ai.tool.name"] == "curl") && '
            'attrs["strathon.tool.args"].contains("http")'
        ),
        tags=["security", "data-exfiltration"],
    ),
    PolicyTemplate(
        id="throttle-expensive-models",
        name="Rate-limit expensive model calls",
        description=(
            "Throttles calls to expensive models (GPT-4o, Claude 3 Opus, "
            "Claude 4 Opus) to 10 calls per minute per agent. Prevents "
            "runaway cost from loops or excessive agency. Covers ASI-06 "
            "(Excessive Agency / Cascading Hallucinations)."
        ),
        owasp_risks=["ASI-06"],
        action="throttle",
        match_expression=(
            'attrs["gen_ai.request.model"] in '
            '["gpt-4o", "gpt-4-turbo", "claude-3-opus-20240229", '
            '"claude-opus-4-20250514", "claude-4-opus"]'
        ),
        action_config={
            "max_calls": 10,
            "window_seconds": 60,
        },
        tags=["cost", "rate-limit"],
    ),
    PolicyTemplate(
        id="block-competitor-email",
        name="Block communication to competitor domains",
        description=(
            "Blocks email or messaging tool calls where the recipient or "
            "arguments contain competitor domain names. Customize the "
            "match_expression to include your competitors. Covers ASI-02 "
            "(Tool Misuse)."
        ),
        owasp_risks=["ASI-02"],
        action="block",
        match_expression=(
            '(attrs["gen_ai.tool.name"] == "send_email" || '
            'attrs["gen_ai.tool.name"] == "send_message") && '
            '(attrs["strathon.tool.args"].contains("@competitor.com") || '
            'attrs["strathon.tool.args"].contains("@rival.com"))'
        ),
        tags=["compliance", "communication"],
    ),
    PolicyTemplate(
        id="block-sql-injection-patterns",
        name="Block SQL injection in tool arguments",
        description=(
            "Blocks tool calls where arguments contain common SQL injection "
            "patterns (DROP TABLE, UNION SELECT, --, ;). Covers ASI-02 "
            "(Tool Misuse) and ASI-05 (Unexpected Code Execution)."
        ),
        owasp_risks=["ASI-02", "ASI-05"],
        action="block",
        match_expression=(
            '(attrs["strathon.tool.args"].contains("DROP TABLE") || '
            'attrs["strathon.tool.args"].contains("UNION SELECT") || '
            'attrs["strathon.tool.args"].contains("DELETE FROM") || '
            'attrs["strathon.tool.args"].contains("; --") || '
            'attrs["strathon.tool.args"].contains("1=1"))'
        ),
        tags=["security", "injection"],
    ),
    PolicyTemplate(
        id="enforce-business-hours",
        name="Block tool calls outside business hours",
        description=(
            "Blocks all tool calls outside Monday-Friday 9am-6pm UTC. "
            "Prevents unattended agents from taking actions when no human "
            "is available for oversight. Customize the hours in the CEL "
            "expression. Covers ASI-10 (Human-Agent Trust Exploitation)."
        ),
        owasp_risks=["ASI-10"],
        action="block",
        match_expression=(
            'now.getDayOfWeek() < 1 || now.getDayOfWeek() > 5 || '
            'now.getHours() < 9 || now.getHours() >= 18'
        ),
        tags=["compliance", "time-based"],
    ),
    PolicyTemplate(
        id="alert-on-high-cost",
        name="Alert when a single call costs over $1",
        description=(
            "Fires a webhook alert when any single LLM call costs more "
            "than $1.00. Cost is computed at ingest from the model price "
            "catalog, so this alerts after the call completes. Does not "
            "block. Covers ASI-06 (Excessive Agency). For hard dollar "
            "spend caps across many calls, use Budgets."
        ),
        owasp_risks=["ASI-06"],
        action="alert",
        match_expression=(
            'double(attrs["gen_ai.usage.cost"]) > 1.0'
        ),
        tags=["cost", "monitoring"],
    ),
    PolicyTemplate(
        id="steer-internal-data",
        name="Steer responses mentioning internal data",
        description=(
            "Replaces tool output with a safe message when the output "
            "contains markers of internal data (CONFIDENTIAL, INTERNAL "
            "ONLY, etc.). Covers ASI-03 (Insecure Output)."
        ),
        owasp_risks=["ASI-03"],
        action="steer",
        match_expression=(
            '(attrs["strathon.tool.output"].contains("CONFIDENTIAL") || '
            'attrs["strathon.tool.output"].contains("INTERNAL ONLY") || '
            'attrs["strathon.tool.output"].contains("DO NOT DISTRIBUTE"))'
        ),
        action_config={
            "replacement": (
                "This information is classified as internal. "
                "Please contact the appropriate team for access."
            ),
        },
        tags=["compliance", "data-protection"],
    ),
    # ---- Memory-poisoning templates (MINJA-class, arXiv:2503.03704) ----
    # These target attacks where adversaries inject malicious content into
    # an agent's retrieval memory to manipulate future behavior: overwrite
    # system instructions, inject prompt-injection payloads, or flood the
    # memory store to dilute legitimate context.
    PolicyTemplate(
        id="block-memory-write-injection",
        name="Block memory writes containing injection patterns",
        description=(
            "Blocks memory-write tool calls when the value being stored "
            "contains common prompt injection patterns (IGNORE PREVIOUS, "
            "system prompt override, role-play injection). Targets MINJA "
            "indirect prompt injection via memory poisoning. "
            "Covers ASI-01 (Prompt Injection), ASI-06 (Data Poisoning)."
        ),
        owasp_risks=["ASI-01", "ASI-06"],
        action="block",
        match_expression=(
            '(attrs["gen_ai.tool.name"] in '
            '["memory_write", "memory_store", "memory_set", "memory_add", '
            '"upsert_memory", "add_memory", "store_memory", "save_memory", '
            '"set_context", "update_context"]) && '
            '(attrs["strathon.tool.args"].contains("IGNORE PREVIOUS") || '
            'attrs["strathon.tool.args"].contains("ignore all previous") || '
            'attrs["strathon.tool.args"].contains("disregard instructions") || '
            'attrs["strathon.tool.args"].contains("you are now") || '
            'attrs["strathon.tool.args"].contains("new system prompt") || '
            'attrs["strathon.tool.args"].contains("override system") || '
            'attrs["strathon.tool.args"].contains("forget everything"))'
        ),
        tags=["security", "memory-poisoning", "prompt-injection"],
    ),
    PolicyTemplate(
        id="block-memory-system-key-overwrite",
        name="Block writes to protected memory keys",
        description=(
            "Blocks memory-write operations targeting system-reserved key "
            "names (system_prompt, instructions, persona, config, role). "
            "Prevents adversaries from overwriting the agent's core "
            "configuration via memory manipulation. "
            "Covers ASI-06 (Data Poisoning), ASI-02 (Tool Misuse)."
        ),
        owasp_risks=["ASI-06", "ASI-02"],
        action="block",
        match_expression=(
            '(attrs["gen_ai.tool.name"] in '
            '["memory_write", "memory_store", "memory_set", "upsert_memory", '
            '"set_context", "update_context"]) && '
            '(attrs["strathon.tool.args"].contains("system_prompt") || '
            'attrs["strathon.tool.args"].contains("system_instructions") || '
            'attrs["strathon.tool.args"].contains("persona") || '
            'attrs["strathon.tool.args"].contains("core_config") || '
            'attrs["strathon.tool.args"].contains("agent_role") || '
            'attrs["strathon.tool.args"].contains("base_instructions"))'
        ),
        tags=["security", "memory-poisoning", "config-protection"],
    ),
    PolicyTemplate(
        id="throttle-memory-write-flood",
        name="Rate-limit memory write operations",
        description=(
            "Throttles memory-write tool calls to prevent memory flooding "
            "attacks where an adversary rapidly inserts many entries to "
            "dilute legitimate context or push genuine memories out of "
            "the retrieval window. "
            "Covers ASI-06 (Data Poisoning), ASI-04 (Resource Overconsumption)."
        ),
        owasp_risks=["ASI-06", "ASI-04"],
        action="throttle",
        match_expression=(
            'attrs["gen_ai.tool.name"] in '
            '["memory_write", "memory_store", "memory_set", "memory_add", '
            '"upsert_memory", "add_memory", "store_memory", "save_memory", '
            '"set_context", "update_context"]'
        ),
        action_config={
            "max_calls": 10,
            "window_seconds": 60,
            "scope": "agent",
        },
        tags=["security", "memory-poisoning", "rate-limit"],
    ),
    PolicyTemplate(
        id="alert-memory-retrieval-injection",
        name="Alert on retrieved memory containing injection patterns",
        description=(
            "Fires an alert webhook when a memory-retrieval tool returns "
            "content containing prompt injection patterns. Indicates the "
            "memory store may already be poisoned and needs review. "
            "Covers ASI-01 (Prompt Injection), ASI-06 (Data Poisoning)."
        ),
        owasp_risks=["ASI-01", "ASI-06"],
        action="alert",
        match_expression=(
            '(attrs["gen_ai.tool.name"] in '
            '["memory_read", "memory_get", "memory_search", "memory_retrieve", '
            '"get_memory", "search_memory", "recall", "get_context"]) && '
            '(attrs["strathon.tool.output"].contains("IGNORE PREVIOUS") || '
            'attrs["strathon.tool.output"].contains("ignore all previous") || '
            'attrs["strathon.tool.output"].contains("disregard instructions") || '
            'attrs["strathon.tool.output"].contains("you are now") || '
            'attrs["strathon.tool.output"].contains("new system prompt"))'
        ),
        tags=["security", "memory-poisoning", "detection"],
    ),
]

TEMPLATES_BY_ID: dict[str, PolicyTemplate] = {t.id: t for t in TEMPLATES}


def list_templates(tag: str | None = None) -> list[dict[str, Any]]:
    """Return the catalog, optionally filtered by tag."""
    result = TEMPLATES
    if tag:
        result = [t for t in result if tag in t.tags]
    return [_serialize(t) for t in result]


def get_template(template_id: str) -> dict[str, Any] | None:
    """Get a single template by ID."""
    t = TEMPLATES_BY_ID.get(template_id)
    return _serialize(t) if t else None


def _serialize(t: PolicyTemplate) -> dict[str, Any]:
    return {
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "owasp_risks": t.owasp_risks,
        "action": t.action,
        "match_expression": t.match_expression,
        "action_config": t.action_config,
        "applies_to": t.applies_to,
        "tags": t.tags,
    }

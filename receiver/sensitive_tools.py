"""Sensitive-tool name heuristic, shared across agent risk scoring, policy
suggestions, and incident detection.

Single source of truth: this used to be defined separately in
api/agent_inventory.py and api/policy_suggestions.py. The two copies had
already drifted -- agent_inventory.py's list was missing "format_disk",
so a tool that policy_suggestions.py correctly flagged as needing coverage
silently didn't count toward an agent's risk score. Import from here
instead of redefining.
"""

SENSITIVE_TOOLS = frozenset({
    "shell_exec", "eval", "exec", "os_system", "subprocess_run",
    "rm", "rmdir", "drop_table", "delete_database", "format_disk",
    "send_email", "send_message", "http_request", "fetch",
    "web_request", "curl", "database_query", "sql_query",
})

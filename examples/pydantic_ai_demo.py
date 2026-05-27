#!/usr/bin/env python3
"""Pydantic AI + Strathon firewall demo.

Demonstrates how Strathon's StrathonFirewall capability intercepts and
blocks tool calls in a Pydantic AI agent based on CEL policy rules.

Unlike other framework integrations, Pydantic AI uses a first-class
capability system — no monkey-patching, no global state.

Prerequisites:
    pip install strathon pydantic-ai-slim

    docker compose up -d  # starts receiver + postgres

Usage:
    # Set a real OpenAI/Anthropic key if you want live LLM responses.
    # The demo works without one (the policy blocks before the tool runs).
    export STRATHON_API_KEY="your-strathon-api-key"
    python pydantic_ai_demo.py
"""

from __future__ import annotations

import os
import sys


def main():
    # ---------- lazy imports so missing deps give clear errors ----------
    try:
        from pydantic_ai import Agent, RunContext
    except ImportError:
        print("ERROR: pydantic-ai not installed. Run: pip install pydantic-ai-slim")
        sys.exit(1)

    try:
        from pydantic_ai.exceptions import SkipToolExecution  # noqa: F401
    except ImportError:
        print("ERROR: pydantic-ai too old. Need >= 1.80.0 for capabilities.")
        sys.exit(1)

    from strathon import Client
    from strathon.instrumentation.pydantic_ai import create_firewall

    # ---------- Strathon client + firewall capability ----------
    client = Client(
        api_key=os.environ.get("STRATHON_API_KEY", "demo-key"),
        endpoint=os.environ.get("STRATHON_ENDPOINT", "http://localhost:4318"),
        project_slug="pydantic-ai-demo",
        enable_policies=True,
    )

    firewall = create_firewall(client)

    # ---------- Agent with tools ----------
    agent = Agent(
        "openai:gpt-4o",
        instructions=(
            "You are a helpful assistant. Use the search tool to answer "
            "questions about the web. Use the file_delete tool to clean up "
            "files when asked."
        ),
        capabilities=[firewall],
    )

    @agent.tool
    def search_web(ctx: RunContext[None], query: str) -> str:
        """Search the web for information."""
        return f"Search results for: {query}"

    @agent.tool
    def file_delete(ctx: RunContext[None], path: str) -> str:
        """Delete a file at the given path."""
        # This tool should be blocked by policy!
        return f"Deleted: {path}"

    @agent.tool
    def read_file(ctx: RunContext[None], path: str) -> str:
        """Read a file at the given path."""
        return f"Contents of {path}: [mock data]"

    # ---------- Simulate policy evaluation ----------
    # In production, policies are fetched from the receiver API.
    # For this demo, we show what happens when check_policy returns
    # a block decision for the file_delete tool.
    print("=" * 60)
    print("Pydantic AI + Strathon Firewall Demo")
    print("=" * 60)
    print()
    print("Agent has 3 tools: search_web, file_delete, read_file")
    print("Policy: block any tool named 'file_delete'")
    print()

    # ---------- Run the agent ----------
    # NOTE: Without a real API key the model call will fail, but
    # the firewall hooks fire on tool calls from the model's response.
    # For a fully self-contained demo, we manually invoke the tool hook
    # to show the policy in action.
    from strathon.policy.types import StrathonPolicyBlocked

    # Simulate what happens when the model tries to call file_delete:
    print("[Simulating model calling file_delete tool...]")
    print()

    try:
        # Build the same span context the firewall would build.
        span_context = {
            "name": "pydantic_ai.tool.file_delete",
            "attrs": {
                "strathon.framework": "pydantic_ai",
                "gen_ai.tool.name": "file_delete",
                "strathon.tool.name": "file_delete",
                "strathon.tool.args": '{"path": "/etc/passwd"}',
            },
        }
        decision = client.check_policy(span_context)
        if decision.is_block:
            print(f"BLOCKED: {decision.message}")
            print(f"  Policy: {decision.policy_name} (id: {decision.policy_id})")
        else:
            print("ALLOWED: file_delete would have run")
    except StrathonPolicyBlocked as e:
        print(f"BLOCKED by policy: {e}")
    except Exception as e:
        # If no policies are configured, the tool would be allowed.
        print(f"No active policies (or receiver unreachable): {e}")
        print("In production, configure a policy via the receiver API:")
        print()
        print('  curl -X POST http://localhost:8000/v1/policies \\')
        print('    -H "Authorization: Bearer $API_KEY" \\')
        print('    -H "Content-Type: application/json" \\')
        print("    -d '{")
        print('      "name": "block-file-delete",')
        print("      \"expression\": \"span.tool.name == 'file_delete'\",")
        print('      "action": "block",')
        print('      "message": "File deletion is not permitted"')
        print("    }'")

    print()
    print("=" * 60)
    print("Integration pattern:")
    print()
    print("  from strathon import Client")
    print("  from strathon.instrumentation.pydantic_ai import create_firewall")
    print()
    print("  client = Client(api_key='...', endpoint='...')")
    print("  firewall = create_firewall(client)")
    print()
    print("  agent = Agent('openai:gpt-4o', capabilities=[firewall])")
    print("=" * 60)

    # ---------- Cleanup ----------
    client.shutdown()


if __name__ == "__main__":
    main()

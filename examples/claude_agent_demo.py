#!/usr/bin/env python3
"""Claude Agent SDK + Strathon firewall demo.

Shows how Strathon instruments the Claude Agent SDK with two layers:

1. Session-level spans: wraps query() and ClaudeSDKClient.query() to
   capture agent sessions (prompt, response, tool usage, metadata).

2. Tool-level enforcement: PreToolUse/PostToolUse hooks on
   ClaudeAgentOptions evaluate CEL policies before each tool call.
   Deny decisions block tool execution before it runs.

Prerequisites:
    pip install strathon claude-agent-sdk
    docker compose up -d

Usage:
    export STRATHON_API_KEY="your-strathon-api-key"
    export ANTHROPIC_API_KEY="your-anthropic-api-key"
    python claude_agent_demo.py
"""

from __future__ import annotations

import os
import sys


def main():
    try:
        from claude_agent_sdk import ClaudeAgentOptions  # noqa: F401
    except ImportError:
        print("ERROR: claude-agent-sdk not installed.")
        print("Run: pip install claude-agent-sdk")
        sys.exit(1)

    from strathon import Client, instrument
    from strathon.instrumentation.claude_agent import create_strathon_hooks

    client = Client(
        api_key=os.environ.get("STRATHON_API_KEY", "demo-key"),
        endpoint=os.environ.get("STRATHON_ENDPOINT", "http://localhost:4318"),
        project_slug="claude-agent-demo",
        enable_policies=True,
    )

    # Layer 1: session-level monkey-patches.
    instrumented = instrument(client, frameworks=["claude_agent"])
    print(f"Instrumented frameworks: {instrumented}")

    # Layer 2: tool-level hooks.
    hooks = create_strathon_hooks(client)

    print()
    print("=" * 60)
    print("Claude Agent SDK + Strathon Firewall Demo")
    print("=" * 60)
    print()
    print("Layer 1 (session-level):")
    print("  query() and ClaudeSDKClient.query() wrapped for OTel spans")
    print("  Captures: prompt, response, session ID, model, max_turns")
    print()
    print("Layer 2 (tool-level hooks):")
    print("  PreToolUse: evaluates CEL policies per tool call")
    print("    - deny: blocks tool execution (rm -rf, etc.)")
    print("    - allow: tool runs normally")
    print("  PostToolUse: emits per-tool OTel spans")
    print()
    print("Integration pattern:")
    print()
    print("  from strathon import Client, instrument")
    print("  from strathon.instrumentation.claude_agent import create_strathon_hooks")
    print()
    print("  client = Client(api_key='...', endpoint='...')")
    print("  instrument(client, frameworks=['claude_agent'])")
    print("  hooks = create_strathon_hooks(client)")
    print()
    print("  # For query() — session-level tracing only:")
    print("  async for msg in query('...'):  # automatically traced")
    print("      print(msg)")
    print()
    print("  # For ClaudeSDKClient — full tool-level enforcement:")
    print("  options = ClaudeAgentOptions(")
    print("      hooks=hooks,")
    print("      allowed_tools=['Read', 'Write', 'Bash'],")
    print("  )")
    print("  async with ClaudeSDKClient(options=options) as sdk:")
    print("      await sdk.query('Review this code')")
    print()
    print(f"Hooks created: {list(hooks.keys())}")
    print("=" * 60)

    client.shutdown()


if __name__ == "__main__":
    main()

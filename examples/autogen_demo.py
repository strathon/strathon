#!/usr/bin/env python3
"""AutoGen + Strathon firewall demo.

Shows how Strathon instruments AutoGen AgentChat with two layers:

1. Agent-level spans: wraps BaseChatAgent.on_messages to capture
   agent name, input/output messages, and token usage.

2. Tool-level enforcement: wraps BaseTool.run_json to evaluate CEL
   policies before each tool call. Block/steer/throttle decisions
   prevent tool execution.

Prerequisites:
    pip install strathon autogen-agentchat autogen-ext[openai]
    docker compose up -d

Usage:
    export STRATHON_API_KEY="your-strathon-api-key"
    export OPENAI_API_KEY="your-openai-api-key"
    python autogen_demo.py
"""

from __future__ import annotations

import os
import sys


def main():
    try:
        from autogen_agentchat.agents import AssistantAgent  # noqa: F401
    except ImportError:
        print("ERROR: autogen-agentchat not installed.")
        print("Run: pip install autogen-agentchat")
        sys.exit(1)

    from strathon import Client, instrument

    client = Client(
        api_key=os.environ.get("STRATHON_API_KEY", "demo-key"),
        endpoint=os.environ.get("STRATHON_ENDPOINT", "http://localhost:4318"),
        project_slug="autogen-demo",
        enable_policies=True,
    )

    instrumented = instrument(client, frameworks=["autogen"])
    print(f"Instrumented frameworks: {instrumented}")

    print()
    print("=" * 60)
    print("AutoGen + Strathon Firewall Demo")
    print("=" * 60)
    print()
    print("Layer 1 (agent-level):")
    print("  BaseChatAgent.on_messages wrapped for OTel spans")
    print("  Captures: agent name, messages, token usage")
    print()
    print("Layer 2 (tool-level):")
    print("  BaseTool.run_json wrapped for policy enforcement")
    print("  Block: raises StrathonPolicyBlocked")
    print("  Steer: returns replacement string")
    print("  Allow: runs tool + emits OTel span")
    print()
    print("Integration pattern:")
    print()
    print("  from strathon import Client, instrument")
    print("  client = Client(")
    print("      api_key='...',")
    print("      endpoint='...',")
    print("      enable_policies=True,")
    print("  )")
    print("  instrument(client, frameworks=['autogen'])")
    print()
    print("  # Define tools and agents as normal.")
    print("  # Strathon automatically enforces policies on every")
    print("  # BaseTool.run_json() call within the agent loop.")
    print()

    # Demonstrate a simple tool definition.
    print("Example tool that would be policy-enforced:")
    print()
    print("  from autogen_core.tools import FunctionTool")
    print()
    print("  def search_web(query: str) -> str:")
    print('      """Search the web."""')
    print('      return f"Results for: {query}"')
    print()
    print("  tool = FunctionTool(search_web, description='Search the web')")
    print()
    print("  # If a policy blocks 'search_web', BaseTool.run_json()")
    print("  # raises StrathonPolicyBlocked before the function runs.")
    print("=" * 60)

    client.shutdown()


if __name__ == "__main__":
    main()

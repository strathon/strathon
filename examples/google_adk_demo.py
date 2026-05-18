#!/usr/bin/env python3
"""Google ADK + Strathon firewall demo.

Demonstrates how Strathon's StrathonFirewallPlugin intercepts and blocks
tool calls in a Google ADK agent based on CEL policy rules.

The plugin uses ADK's first-class BasePlugin system — no monkey-patching.
The before_tool_callback returns a dict to short-circuit tool execution
when a policy matches.

Prerequisites:
    pip install strathon google-adk

    docker compose up -d  # starts receiver + postgres

Usage:
    export STRATHON_API_KEY="your-strathon-api-key"
    export GOOGLE_API_KEY="your-gemini-api-key"
    python google_adk_demo.py
"""

from __future__ import annotations

import os
import sys


def main():
    try:
        from google.adk.agents import LlmAgent  # noqa: F401
    except ImportError:
        print("ERROR: google-adk not installed. Run: pip install google-adk")
        sys.exit(1)

    from strathon import Client
    from strathon.instrumentation.google_adk import create_firewall_plugin

    # ---------- Strathon client + plugin ----------
    client = Client(
        api_key=os.environ.get("STRATHON_API_KEY", "demo-key"),
        endpoint=os.environ.get("STRATHON_ENDPOINT", "http://localhost:4318"),
        project_slug="google-adk-demo",
        enable_policies=True,
    )

    plugin = create_firewall_plugin(client)

    # ---------- Demo output ----------
    print("=" * 60)
    print("Google ADK + Strathon Firewall Demo")
    print("=" * 60)
    print()
    print(f"Plugin registered: {plugin.name}")
    print("Block mechanism: before_tool_callback returns dict")
    print()
    print("Integration pattern:")
    print()
    print("  from strathon import Client")
    print("  from strathon.instrumentation.google_adk import create_firewall_plugin")
    print()
    print("  client = Client(api_key='...', endpoint='...')")
    print("  plugin = create_firewall_plugin(client)")
    print()
    print("  runner = Runner(")
    print("      agent=my_agent,")
    print("      app_name='my-app',")
    print("      session_service=session_service,")
    print("      plugins=[plugin],")
    print("  )")
    print()
    print("Policy enforcement flow:")
    print("  1. Model decides to call a tool")
    print("  2. before_tool_callback evaluates CEL policies")
    print("  3. Block: returns {'error': '...', 'blocked_by': 'strathon_policy'}")
    print("  4. Steer: returns {'result': '...', 'steered_by': 'strathon_policy'}")
    print("  5. Allow: returns None, tool executes normally")
    print("  6. after_tool_callback emits OTel span to receiver")
    print()
    print("Known caveats:")
    print("  - VertexAiRagRetrieval may bypass plugin callbacks (ADK #2629)")
    print("  - Plugins don't propagate to sub-agents via AgentTool (ADK #2809)")
    print("=" * 60)

    client.shutdown()


if __name__ == "__main__":
    main()

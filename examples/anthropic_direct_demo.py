#!/usr/bin/env python3
"""Anthropic direct SDK + Strathon observability demo.

Shows how Strathon captures OpenTelemetry spans for every
anthropic.messages.create() call, including streaming responses.

Spans include: model, tokens, prompt, completion, tool use, cache stats.

Prerequisites:
    pip install strathon anthropic
    docker compose up -d

Usage:
    export STRATHON_API_KEY="your-strathon-api-key"
    export ANTHROPIC_API_KEY="your-anthropic-api-key"
    python anthropic_direct_demo.py
"""

from __future__ import annotations

import os
import sys


def main():
    try:
        import anthropic  # noqa: F401
    except ImportError:
        print("ERROR: anthropic not installed. Run: pip install anthropic")
        sys.exit(1)

    from strathon import Client, instrument

    client = Client(
        api_key=os.environ.get("STRATHON_API_KEY", "demo-key"),
        endpoint=os.environ.get("STRATHON_ENDPOINT", "http://localhost:4318"),
        project_slug="anthropic-direct-demo",
    )

    instrumented = instrument(client, frameworks=["anthropic"])
    print(f"Instrumented frameworks: {instrumented}")

    print()
    print("=" * 60)
    print("Anthropic Direct SDK + Strathon Demo")
    print("=" * 60)
    print()
    print("Every anthropic.messages.create() call now emits an OTel")
    print("span to the Strathon receiver with:")
    print("  - gen_ai.request.model")
    print("  - gen_ai.usage.input_tokens / output_tokens")
    print("  - gen_ai.usage.cache_read.input_tokens (if applicable)")
    print("  - gen_ai.prompt (truncated)")
    print("  - gen_ai.completion (truncated)")
    print("  - gen_ai.response.tool_calls (if any)")
    print("  - Streaming responses tracked via event accumulation")
    print()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key and api_key != "your-anthropic-api-key":
        anth = anthropic.Anthropic(api_key=api_key)
        print("Making a live Anthropic API call...")
        response = anth.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=50,
            messages=[{"role": "user", "content": "Say hello in 5 words."}],
        )
        print(f"Response: {response.content[0].text}")
        print()
        print("Check your Strathon dashboard for the captured span.")
    else:
        print("No ANTHROPIC_API_KEY set. Set it to see a live trace.")
        print("The instrumentation is active regardless.")

    print()
    print("Integration pattern:")
    print()
    print("  from strathon import Client, instrument")
    print("  client = Client(api_key='...', endpoint='...')")
    print("  instrument(client, frameworks=['anthropic'])")
    print("  # All anthropic.messages.create() calls are now traced.")
    print("=" * 60)

    client.shutdown()


if __name__ == "__main__":
    main()

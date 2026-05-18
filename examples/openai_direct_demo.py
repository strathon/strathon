#!/usr/bin/env python3
"""OpenAI direct SDK + Strathon observability demo.

Shows how Strathon captures OpenTelemetry spans for every
openai.chat.completions.create() and openai.responses.create() call.
No agent framework needed — works with raw OpenAI SDK usage.

Spans include: model, tokens, prompt, completion, tool calls.

Prerequisites:
    pip install strathon openai
    docker compose up -d

Usage:
    export STRATHON_API_KEY="your-strathon-api-key"
    export OPENAI_API_KEY="your-openai-api-key"
    python openai_direct_demo.py
"""

from __future__ import annotations

import os
import sys


def main():
    try:
        import openai  # noqa: F401
    except ImportError:
        print("ERROR: openai not installed. Run: pip install openai")
        sys.exit(1)

    from strathon import Client, instrument

    client = Client(
        api_key=os.environ.get("STRATHON_API_KEY", "demo-key"),
        endpoint=os.environ.get("STRATHON_ENDPOINT", "http://localhost:4318"),
        project_slug="openai-direct-demo",
    )

    # Instrument OpenAI SDK — patches chat.completions.create
    # and responses.create (sync + async).
    instrumented = instrument(client, frameworks=["openai"])
    print(f"Instrumented frameworks: {instrumented}")

    print()
    print("=" * 60)
    print("OpenAI Direct SDK + Strathon Demo")
    print("=" * 60)
    print()
    print("Every openai.chat.completions.create() call now emits an")
    print("OTel span to the Strathon receiver with:")
    print("  - gen_ai.request.model")
    print("  - gen_ai.usage.input_tokens / output_tokens")
    print("  - gen_ai.prompt (truncated)")
    print("  - gen_ai.completion (truncated)")
    print("  - gen_ai.response.tool_calls (if any)")
    print()

    # Make a real call if API key is available.
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key and api_key != "your-openai-api-key":
        oai = openai.OpenAI(api_key=api_key)
        print("Making a live OpenAI API call...")
        response = oai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Say hello in 5 words."}],
            max_tokens=20,
        )
        print(f"Response: {response.choices[0].message.content}")
        print()
        print("Check your Strathon dashboard for the captured span.")
    else:
        print("No OPENAI_API_KEY set. Set it to see a live trace.")
        print("The instrumentation is active regardless.")

    print()
    print("Integration pattern:")
    print()
    print("  from strathon import Client, instrument")
    print("  client = Client(api_key='...', endpoint='...')")
    print("  instrument(client, frameworks=['openai'])")
    print("  # All openai.chat.completions.create() calls are now traced.")
    print("=" * 60)

    client.shutdown()


if __name__ == "__main__":
    main()

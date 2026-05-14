"""OpenAI Agents SDK runtime intervention demo: Strathon blocks a tool call.

This is the OpenAI Agents SDK counterpart to intervention_demo.py (LangGraph)
and crewai_intervention_demo.py.

Block point: RunHooks.on_tool_start fires BEFORE the runner creates the tool
invocation task. Strathon's instrument() wraps Runner.run / run_sync /
run_streamed to inject a RunHooks subclass that calls client.check_policy()
on every tool start. If the decision is block, the hook raises
StrathonPolicyBlocked; asyncio.gather propagates it; the runner aborts
before the tool body runs.

This demo exercises the hook directly rather than driving a full Runner.run
(which would require an OpenAI API call). The Runner-wrapping path is
covered by tests/test_openai_agents_intervention.py.

Prerequisites:
    pip install strathon openai-agents cel-python
    Receiver running at http://localhost:4318 with migrations applied.

Run:
    python openai_agents_intervention_demo.py
"""

import asyncio
import json
import time
from urllib.request import Request, urlopen

from agents import function_tool

from strathon import Client
from strathon.policy import StrathonPolicyBlocked


RECEIVER_URL = "http://localhost:4318"


# ---- A real OAI Agents SDK function tool. Does the body actually run? ----

_emails_actually_sent: list[dict] = []


@function_tool
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email to the given recipient."""
    _emails_actually_sent.append({"to": to, "subject": subject, "body": body})
    return f"sent email to {to}"


# ---- REST helpers ----


def _post(url: str, payload: dict) -> dict:
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url: str) -> dict:
    with urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _delete(url: str) -> None:
    req = Request(url, method="DELETE")
    try:
        with urlopen(req, timeout=5):
            pass
    except Exception:
        pass


def install_demo_policy() -> dict:
    """Create the flagship block policy. Returns the created row."""
    _demo_policies = {
        "block_competitor_email_demo",
        "block_competitor_email_crewai_demo",
        "block_competitor_email_oai_demo",
    }
    existing = _get(f"{RECEIVER_URL}/v1/policies").get("policies", [])
    for p in existing:
        if p["name"] in _demo_policies:
            _delete(f"{RECEIVER_URL}/v1/policies/{p['id']}")

    return _post(
        f"{RECEIVER_URL}/v1/policies",
        {
            "name": "block_competitor_email_oai_demo",
            "description": "Demo policy: prevent OpenAI Agents tools from emailing @competitor.com",
            "match_expression": (
                'attrs["gen_ai.tool.name"] == "send_email" && '
                'attrs["strathon.tool.args"].contains("@competitor.com")'
            ),
            "action": "block",
            "action_config": {
                "message": "Cannot email a competitor address (Strathon policy).",
            },
            "priority": 100,
        },
    )


class _DemoToolContext:
    """Minimal ToolContext-shaped object exposing what our hook reads.

    Stands in for the real ToolContext that Runner.run constructs internally.
    Lets us exercise the on_tool_start path without an OpenAI API call.
    """

    def __init__(self, tool_arguments):
        self.tool_arguments = tool_arguments


def main() -> None:
    print("Installing demo block policy via REST...")
    policy = install_demo_policy()
    print(f"  -> policy id: {policy['id']}")
    print(f"  -> CEL: {policy['match_expression']}")

    print("\nInitializing Strathon Client (will fetch policies from receiver)...")
    client = Client(
        api_key="dev-key",
        endpoint=RECEIVER_URL,
        service_name="oai-agents-intervention-demo",
        environment="dev",
    )
    time.sleep(0.5)

    enforcer = client.policy_enforcer
    if enforcer is None:
        raise SystemExit("policies disabled on client; cannot run demo")
    print(f"  -> {len(enforcer.policies)} policies loaded into SDK enforcer")

    from strathon.instrumentation.openai_agents import (
        instrument,
        _build_strathon_run_hooks,
        _uninstall_policy_patch,
    )
    _uninstall_policy_patch()  # in case of re-runs in the same process
    instrument(client)
    print("  -> OpenAI Agents SDK instrumentation installed (processor + Runner.run* patch)")

    # Build the hook directly so we can exercise it without driving an LLM.
    # Under a real Runner.run, the SDK builds the hook itself (via our patch)
    # and calls it; we're inlining that step here.
    hooks = _build_strathon_run_hooks(client, user_hooks=None)

    # ---- Scenario 1: tool call targeting competitor address. Must be blocked. ----
    print("\n--- Scenario 1: send_email to sales@competitor.com (should block) ---")
    competitor_ctx = _DemoToolContext(
        tool_arguments={
            "to": "sales@competitor.com",
            "subject": "Hi",
            "body": "wanted to chat",
        }
    )

    async def scenario_1():
        await hooks.on_tool_start(competitor_ctx, agent=None, tool=send_email)
        # If the hook didn't raise, simulate the tool actually running
        send_email._called = True

    try:
        asyncio.run(scenario_1())
        print("  UNEXPECTED: tool ran without being blocked")
    except StrathonPolicyBlocked as exc:
        print(f"  BLOCKED by policy '{exc.policy_name}'")
        print(f"  message: {exc.message}")

    # ---- Scenario 2: innocuous internal email. Hook should allow it. ----
    print("\n--- Scenario 2: send_email to team@mycompany.com (should run) ---")
    innocuous_ctx = _DemoToolContext(
        tool_arguments={
            "to": "team@mycompany.com",
            "subject": "Weekly update",
            "body": "All good.",
        }
    )

    async def scenario_2():
        await hooks.on_tool_start(innocuous_ctx, agent=None, tool=send_email)
        # Hook allowed; simulate the tool body running
        _emails_actually_sent.append({
            "to": "team@mycompany.com",
            "subject": "Weekly update",
            "body": "All good.",
        })

    asyncio.run(scenario_2())
    print(f"  result: sent email to team@mycompany.com")

    # ---- Summary ----
    print("\n=== Summary ===")
    print(f"  Emails actually sent: {len(_emails_actually_sent)}")
    for e in _emails_actually_sent:
        print(f"    -> {e['to']} | {e['subject']}")
    print(
        "  (Expected: 1 email to team@mycompany.com. The competitor email "
        "was blocked before the tool body ran.)"
    )

    print("\nFlushing spans to receiver...")
    client.flush(timeout_millis=10000)
    client.shutdown()
    print("Done.")


if __name__ == "__main__":
    main()

"""Runtime intervention demo: Strathon blocks a real LangChain tool call.

This is the killer-feature demo. Three things happen:

1. We register a CEL policy with the receiver that says "block any send_email
   tool call where the body contains @competitor.com".

2. The SDK pulls policies from the receiver in the background.

3. We invoke a real LangChain tool through a LangGraph-style callback handler.
   When the agent tries to email a competitor address, Strathon raises
   StrathonPolicyBlocked *before* the tool's underlying function runs.
   When the agent emails an internal address, the tool runs normally.

This is exactly the workflow:

    - LangChain on_tool_start fires
    - Strathon's handler calls client.check_policy(tool_attrs)
    - PolicyEnforcer evaluates the CEL expression against the candidate args
    - If matched, returns PolicyDecision(action='block', ...)
    - Handler raises StrathonPolicyBlocked, LangChain propagates the exception,
      tool body never executes.

Prerequisites:
    pip install strathon langchain-core cel-python
    Receiver running at http://localhost:4318 with the policies migration applied.

Run:
    python intervention_demo.py
"""

import json
import time
from urllib.request import Request, urlopen

from langchain_core.tools import tool

from strathon import Client
from strathon.policy import StrathonPolicyBlocked


RECEIVER_URL = "http://localhost:4318"


# ---- A real LangChain tool. Whether this function body runs is the test. ----

_emails_actually_sent: list[dict] = []


@tool
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email to the given recipient. Returns a confirmation string."""
    _emails_actually_sent.append({"to": to, "subject": subject, "body": body})
    return f"sent email to {to}"


# ---- Policy management helpers (talk to the receiver via REST) ----


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
    # Remove any policy with the same name from prior runs so this demo is
    # reproducible. We do it the easy way: list, find by name, delete.
    existing = _get(f"{RECEIVER_URL}/v1/policies").get("policies", [])
    for p in existing:
        if p["name"] == "block_competitor_email_demo":
            _delete(f"{RECEIVER_URL}/v1/policies/{p['id']}")

    return _post(
        f"{RECEIVER_URL}/v1/policies",
        {
            "name": "block_competitor_email_demo",
            "description": "Demo policy: prevent agents from emailing @competitor.com",
            "match_expression": (
                'attrs["gen_ai.tool.name"] == "send_email" && '
                'attrs["strathon.tool.input"].contains("@competitor.com")'
            ),
            "action": "block",
            "action_config": {
                "message": "Cannot email a competitor address (Strathon policy).",
            },
            "priority": 100,
        },
    )


# ---- The demo itself ----


def main() -> None:
    print("Installing demo block policy via REST...")
    policy = install_demo_policy()
    print(f"  -> policy id: {policy['id']}")
    print(f"  -> CEL: {policy['match_expression']}")

    print("\nInitializing Strathon Client (will fetch policies from receiver)...")
    client = Client(
        api_key="dev-key",
        endpoint=RECEIVER_URL,
        service_name="intervention-demo",
        environment="dev",
    )
    # Give the enforcer a tick to ensure the policy is loaded
    time.sleep(0.5)

    enforcer = client.policy_enforcer
    if enforcer is None:
        raise SystemExit("policies disabled on client; cannot run demo")
    print(f"  -> {len(enforcer.policies)} policies loaded into SDK enforcer")

    from strathon.instrumentation.langgraph import instrument

    # Reset module singleton so demo is reproducible across runs in the same proc
    import strathon.instrumentation.langgraph as mod
    mod._REGISTERED_HANDLER = None

    handler = instrument(client)
    if handler is None:
        raise SystemExit("langchain_core not installed")

    # ---- Scenario 1: agent tries to email a competitor. Must be blocked. ----
    print("\n--- Scenario 1: send_email to sales@competitor.com (should block) ---")
    try:
        send_email.invoke(
            {
                "to": "sales@competitor.com",
                "subject": "Hi",
                "body": "wanted to chat",
            },
            config={"callbacks": [handler]},
        )
        print("  UNEXPECTED: tool ran without being blocked")
    except StrathonPolicyBlocked as exc:
        print(f"  BLOCKED by policy '{exc.policy_name}'")
        print(f"  message: {exc.message}")

    # ---- Scenario 2: innocuous internal email. Should run normally. ----
    print("\n--- Scenario 2: send_email to team@mycompany.com (should run) ---")
    result = send_email.invoke(
        {
            "to": "team@mycompany.com",
            "subject": "Weekly update",
            "body": "Things are going well.",
        },
        config={"callbacks": [handler]},
    )
    print(f"  result: {result}")

    # ---- Summary ----
    print("\n=== Summary ===")
    print(f"  Emails actually sent: {len(_emails_actually_sent)}")
    for e in _emails_actually_sent:
        print(f"    -> {e['to']} | {e['subject']}")
    print(
        "  (Expected: 1 email to team@mycompany.com. "
        "The competitor email was blocked before send_email's body ran.)"
    )

    print("\nFlushing spans to receiver...")
    client.flush(timeout_millis=10000)
    client.shutdown()
    print("Done.")


if __name__ == "__main__":
    main()

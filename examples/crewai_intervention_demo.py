"""CrewAI runtime intervention demo: Strathon blocks a real Crew tool call.

This is the CrewAI counterpart to examples/intervention_demo.py. Three things
happen:

1. A CEL policy is registered with the receiver: "block any send_email tool
   call where the input contains @competitor.com".

2. The SDK pulls policies from the receiver in the background.

3. A real CrewAI tool (built from BaseTool, wrapped to CrewStructuredTool) is
   invoked twice. The first invocation targets a competitor address and is
   blocked before the tool body runs. The second invocation targets an internal
   address and runs normally.

The block point for CrewAI is `CrewStructuredTool.invoke`, patched by
strathon.instrumentation.crewai.instrument() at install time. The patch
calls client.check_policy() before delegating to the original method.

Prerequisites:
    pip install strathon crewai cel-python
    Receiver running at http://localhost:4318 with migrations applied.

Run:
    python crewai_intervention_demo.py
"""

import json
import time
from urllib.request import Request, urlopen

from crewai.tools import BaseTool

from strathon import Client
from strathon.policy import StrathonPolicyBlocked


RECEIVER_URL = "http://localhost:4318"


# ---- A real CrewAI tool. Whether _run executes is the test. ----

_emails_actually_sent: list[dict] = []


class SendEmailTool(BaseTool):
    name: str = "send_email"
    description: str = "Send an email to the given recipient."

    def _run(self, to: str = "", subject: str = "", body: str = "") -> str:
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
    existing = _get(f"{RECEIVER_URL}/v1/policies").get("policies", [])
    for p in existing:
        if p["name"] == "block_competitor_email_crewai_demo":
            _delete(f"{RECEIVER_URL}/v1/policies/{p['id']}")

    return _post(
        f"{RECEIVER_URL}/v1/policies",
        {
            "name": "block_competitor_email_crewai_demo",
            "description": "Demo policy: prevent CrewAI tools from emailing @competitor.com",
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


def main() -> None:
    print("Installing demo block policy via REST...")
    policy = install_demo_policy()
    print(f"  -> policy id: {policy['id']}")
    print(f"  -> CEL: {policy['match_expression']}")

    print("\nInitializing Strathon Client (will fetch policies from receiver)...")
    client = Client(
        api_key="dev-key",
        endpoint=RECEIVER_URL,
        service_name="crewai-intervention-demo",
        environment="dev",
    )
    time.sleep(0.5)

    enforcer = client.policy_enforcer
    if enforcer is None:
        raise SystemExit("policies disabled on client; cannot run demo")
    print(f"  -> {len(enforcer.policies)} policies loaded into SDK enforcer")

    from strathon.instrumentation.crewai import instrument
    import strathon.instrumentation.crewai as mod
    # Make demo reproducible across re-runs in the same process
    mod._uninstall_policy_patch()
    mod._REGISTERED_LISTENER = None

    instrument(client)
    print("  -> CrewAI instrumentation installed (event listener + policy patch)")

    # Build a real CrewAI tool. CrewAI's tool execution path goes through
    # CrewStructuredTool.invoke, which is what we patch.
    structured = SendEmailTool().to_structured_tool()

    # ---- Scenario 1: tool call targeting competitor address. Must be blocked. ----
    print("\n--- Scenario 1: send_email to sales@competitor.com (should block) ---")
    try:
        structured.invoke({"to": "sales@competitor.com", "subject": "Hi", "body": "wanted to chat"})
        print("  UNEXPECTED: tool ran without being blocked")
    except StrathonPolicyBlocked as exc:
        print(f"  BLOCKED by policy '{exc.policy_name}'")
        print(f"  message: {exc.message}")

    # ---- Scenario 2: tool call to an internal address. Should run normally. ----
    print("\n--- Scenario 2: send_email to team@mycompany.com (should run) ---")
    result = structured.invoke(
        {"to": "team@mycompany.com", "subject": "Weekly update", "body": "All good."}
    )
    print(f"  result: {result}")

    # ---- Summary ----
    print("\n=== Summary ===")
    print(f"  Emails actually sent: {len(_emails_actually_sent)}")
    for e in _emails_actually_sent:
        print(f"    -> {e['to']} | {e['subject']}")
    print(
        "  (Expected: 1 email to team@mycompany.com. "
        "The competitor email was blocked before SendEmailTool._run ran.)"
    )

    print("\nFlushing spans to receiver...")
    client.flush(timeout_millis=10000)
    client.shutdown()
    print("Done.")


if __name__ == "__main__":
    main()

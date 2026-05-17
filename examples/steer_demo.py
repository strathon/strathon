"""Steer demo: Strathon replaces a dangerous tool output with a safe alternative.

Block stops the tool call entirely. Steer is more surgical: the tool runs,
but Strathon intercepts the output and replaces it with a corrective string.
Use cases:

  - A customer-support agent tries to reveal internal pricing — steer replaces
    the output with "I can't share internal pricing. Let me connect you with sales."
  - A code agent generates SQL with DROP TABLE — steer replaces it with a
    safe SELECT query.
  - A research agent returns a copyrighted passage — steer replaces it with
    a summary.

This demo:

1. Creates a steer policy: any tool named "lookup_pricing" that returns
   a string containing "internal" gets its output replaced.
2. Calls the tool with an internal pricing query → output is replaced.
3. Calls the tool with a public pricing query → output passes through.

Prerequisites:
    pip install strathon langchain-core cel-python
    Receiver running at http://localhost:4318

Run:
    python steer_demo.py
"""

import json
import time
from urllib.request import Request, urlopen

from langchain_core.tools import tool

from strathon import Client, instrument


RECEIVER_URL = "http://localhost:4318"
API_KEY = "stra_dev_local_default_project_do_not_use_in_production"
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}"}


def _api(method, path, body=None):
    url = f"{RECEIVER_URL}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {**AUTH_HEADERS, "Content-Type": "application/json"}
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw.strip() else {}
    except Exception as e:
        return getattr(e, "code", 0), {}


# ---- The tool under test ----

_PRICING_DB = {
    "enterprise": "Internal: $50/seat/month, 80% margin, negotiable to $30",
    "starter": "Public: $10/seat/month, see getstrathon.com/pricing",
}


@tool
def lookup_pricing(plan: str) -> str:
    """Look up pricing for a plan."""
    return _PRICING_DB.get(plan, f"No pricing found for plan: {plan}")


def create_steer_policy():
    """Create a policy that steers internal pricing lookups."""
    status, body = _api("POST", "/v1/policies", {
        "name": "steer-internal-pricing",
        "match_expression": (
            'attrs["gen_ai.tool.name"] == "lookup_pricing" && '
            'attrs["strathon.tool.args"].contains("enterprise")'
        ),
        "action": "steer",
        "action_config": {
            "replacement": "I can't share internal pricing details. "
                          "Please visit getstrathon.com/pricing or contact sales@getstrathon.com "
                          "for enterprise pricing."
        },
    })
    policy_id = body.get("id")
    print(f"  Created steer policy (id={policy_id})")
    return policy_id


def main():
    print("\n=== Strathon Steer Demo ===\n")

    print("[1] Creating steer policy for internal pricing...")
    policy_id = create_steer_policy()

    print("\n[2] Initializing SDK...")
    client = Client(api_key=API_KEY, endpoint=RECEIVER_URL)
    instrument(client, frameworks=["langchain"])
    # Wait for policy sync.
    time.sleep(1.5)

    print("\n[3] Looking up 'enterprise' plan (internal pricing)...")
    try:
        result = lookup_pricing.invoke({"plan": "enterprise"})
        if "can't share" in result.lower() or "contact sales" in result.lower():
            print(f"  STEERED: {result}")
        else:
            print(f"  Result: {result}")
            print("  (Policy sync may not have completed — steer would replace this)")
    except Exception as e:
        print(f"  Exception: {e}")

    print("\n[4] Looking up 'starter' plan (public pricing)...")
    try:
        result = lookup_pricing.invoke({"plan": "starter"})
        print(f"  PASSED THROUGH: {result}")
    except Exception as e:
        print(f"  Exception: {e}")

    print("\n[5] Cleaning up...")
    if policy_id:
        _api("DELETE", f"/v1/policies/{policy_id}")

    print("\n=== Demo complete ===")
    print("The enterprise pricing query was steered to a safe response.")
    print("The public pricing query passed through unchanged.")
    print("Steer lets you keep the agent running while controlling what it says.\n")


if __name__ == "__main__":
    main()

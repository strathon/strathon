"""Allow-list demo: deny everything except explicitly approved tools.

Default mode: deny unless a policy explicitly allows. This inverts the
normal "allow unless blocked" model. Use cases:

  - Compliance-regulated agents (finance, healthcare) that can only use
    pre-approved tools.
  - Production agents where new tools must pass review before activation.
  - Zero-trust agent deployments.

This demo:

1. Enables allow-list mode on the project (intervention_default_action = "deny").
2. Creates an allow policy for "search_docs" only.
3. Calls search_docs → allowed, runs normally.
4. Calls send_email → denied, raises StrathonPolicyBlocked.

The agent can only do what you've explicitly permitted.

Prerequisites:
    pip install strathon langchain-core cel-python
    Receiver running at http://localhost:4318

Run:
    python allowlist_demo.py
"""

import json
import time
from urllib.request import Request, urlopen

from langchain_core.tools import tool

from strathon import Client, instrument
from strathon.policy import StrathonPolicyBlocked


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


# ---- Tools ----

@tool
def search_docs(query: str) -> str:
    """Search the knowledge base."""
    return f"Found 3 results for '{query}': [doc1, doc2, doc3]"


@tool
def send_email(to: str, body: str) -> str:
    """Send an email. This tool is NOT on the allow-list."""
    return f"Email sent to {to}"


def enable_allowlist_mode():
    """Set the project to deny-by-default (allow-list mode)."""
    status, body = _api("PATCH", "/v1/project/settings", {
        "intervention_default_action": "deny",
    })
    print(f"  Project default action: {body.get('intervention_default_action', '?')}")


def disable_allowlist_mode():
    """Reset to normal allow-by-default mode."""
    _api("PATCH", "/v1/project/settings", {
        "intervention_default_action": "allow",
    })


def create_allow_policy():
    """Allow only the search_docs tool."""
    status, body = _api("POST", "/v1/policies", {
        "name": "allow-search-docs",
        "match_expression": 'attrs["gen_ai.tool.name"] == "search_docs"',
        "action": "allow",
    })
    policy_id = body.get("id")
    print(f"  Created allow policy for search_docs (id={policy_id})")
    return policy_id


def main():
    print("\n=== Strathon Allow-List Demo ===\n")

    print("[1] Enabling allow-list mode (deny by default)...")
    enable_allowlist_mode()

    print("\n[2] Creating allow policy for 'search_docs' only...")
    policy_id = create_allow_policy()

    print("\n[3] Initializing SDK...")
    client = Client(api_key=API_KEY, endpoint=RECEIVER_URL)
    instrument(client, frameworks=["langchain"])
    time.sleep(1.5)

    print("\n[4] Calling search_docs (on the allow-list)...")
    try:
        result = search_docs.invoke({"query": "deployment guide"})
        print(f"  ALLOWED: {result}")
    except StrathonPolicyBlocked as e:
        print(f"  BLOCKED (unexpected): {e}")

    print("\n[5] Calling send_email (NOT on the allow-list)...")
    try:
        result = send_email.invoke({"to": "ceo@competitor.com", "body": "Hi"})
        print(f"  Result: {result}")
        print("  (If this ran, policy sync may not have completed yet)")
    except StrathonPolicyBlocked as e:
        print(f"  DENIED: {e}")
        print("  send_email is not on the allow-list — blocked before execution.")

    print("\n[6] Cleaning up...")
    if policy_id:
        _api("DELETE", f"/v1/policies/{policy_id}")
    disable_allowlist_mode()

    print("\n=== Demo complete ===")
    print("In allow-list mode, only search_docs was permitted.")
    print("send_email was denied because it wasn't explicitly allowed.")
    print("Zero-trust for AI agents: nothing runs unless you approve it.\n")


if __name__ == "__main__":
    main()

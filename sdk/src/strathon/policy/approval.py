"""Human approval workflow for the SDK.

When a policy returns ``require_approval``, the framework integration
calls ``wait_for_approval`` which:

1. POSTs to the receiver to create a pending approval record.
2. Polls ``GET /v1/approvals/{id}`` until the status changes from
   ``pending`` to ``approved``, ``denied``, or ``expired``.
3. Returns True (approved) or raises ``StrathonApprovalDenied``.

The poll loop respects the policy's ``timeout_seconds``. If the
receiver is unreachable, the configurable ``on_timeout`` behavior
applies (default: deny).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from strathon.policy.types import PolicyDecision, StrathonApprovalDenied

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL = 2.0  # seconds between polls


def request_approval(
    client,
    decision: PolicyDecision,
    span_context: Dict[str, Any],
) -> Optional[str]:
    """Create a pending approval on the receiver. Returns the approval_id.

    Makes a synchronous POST to /v1/approvals. Returns None if the
    request fails (caller should fall back to on_error behavior).
    """
    endpoint = getattr(client, "_endpoint", None) or "http://localhost:8000"
    # Strip trailing /v1/traces or similar from the OTLP endpoint
    # to get the base receiver URL.
    base = endpoint.rstrip("/")
    if base.endswith("/v1/traces"):
        base = base[: -len("/v1/traces")]
    elif base.endswith("/v1"):
        base = base[: -len("/v1")]

    url = f"{base}/v1/approvals"
    attrs = span_context.get("attrs", {})
    body = json.dumps({
        "policy_id": decision.policy_id,
        "policy_name": decision.policy_name,
        "span_name": span_context.get("name"),
        "tool_name": attrs.get("gen_ai.tool.name") or attrs.get("strathon.tool.name"),
        "tool_args": attrs.get("strathon.tool.args"),
        "timeout_seconds": decision.timeout_seconds or 300,
    }).encode("utf-8")

    api_key = getattr(client, "_api_key", None)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        req = Request(url, data=body, headers=headers, method="POST")
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            approval = data.get("approval", data)
            return approval.get("id")
    except Exception:
        logger.exception("Failed to create approval on receiver")
        return None


def poll_approval(
    client,
    approval_id: str,
    timeout_seconds: int = 300,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
) -> str:
    """Poll the receiver until the approval resolves. Returns final status.

    Returns one of: 'approved', 'denied', 'expired', 'timeout' (local).
    """
    endpoint = getattr(client, "_endpoint", None) or "http://localhost:8000"
    base = endpoint.rstrip("/")
    if base.endswith("/v1/traces"):
        base = base[: -len("/v1/traces")]
    elif base.endswith("/v1"):
        base = base[: -len("/v1")]

    url = f"{base}/v1/approvals/{approval_id}"
    api_key = getattr(client, "_api_key", None)
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        try:
            req = Request(url, headers=headers, method="GET")
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                approval = data.get("approval", data)
                status = approval.get("status", "pending")
                if status != "pending":
                    return status
        except URLError:
            logger.debug("Approval poll failed (receiver unreachable), retrying")
        except Exception:
            logger.debug("Approval poll error", exc_info=True)

        remaining = deadline - time.monotonic()
        sleep_time = min(poll_interval, max(remaining, 0))
        if sleep_time > 0:
            time.sleep(sleep_time)

    return "timeout"


def wait_for_approval(
    client,
    decision: PolicyDecision,
    span_context: Dict[str, Any],
    on_timeout: str = "deny",
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
) -> bool:
    """Full approval workflow: create + poll + resolve.

    Returns True if approved. Raises StrathonApprovalDenied if denied,
    expired, or timed out (when on_timeout="deny").

    Args:
        client: Strathon Client instance.
        decision: The require_approval PolicyDecision from check_policy.
        span_context: The span context dict that triggered the policy.
        on_timeout: "deny" (default) or "allow". What to do when the
            approval times out without a response.
        poll_interval: Seconds between status polls.

    Returns:
        True if the approval was granted.

    Raises:
        StrathonApprovalDenied: If denied, expired, or timed out with
            on_timeout="deny".
    """
    timeout = decision.timeout_seconds or 300

    # Step 1: Create the approval on the receiver.
    approval_id = request_approval(client, decision, span_context)
    if approval_id is None:
        # Receiver unreachable — apply on_timeout behavior.
        if on_timeout == "allow":
            logger.warning(
                "Could not create approval (receiver unreachable); "
                "allowing per on_timeout=allow"
            )
            return True
        raise StrathonApprovalDenied(
            "Could not create approval on receiver",
            policy_id=decision.policy_id,
            policy_name=decision.policy_name,
            status="error",
        )

    logger.info(
        "Approval %s created for policy %s; waiting up to %ds",
        approval_id, decision.policy_name, timeout,
    )

    # Step 2: Poll until resolved.
    status = poll_approval(
        client, approval_id,
        timeout_seconds=timeout,
        poll_interval=poll_interval,
    )

    # Step 3: Act on the result.
    if status == "approved":
        logger.info("Approval %s granted", approval_id)
        return True

    if status == "timeout" and on_timeout == "allow":
        logger.warning(
            "Approval %s timed out; allowing per on_timeout=allow",
            approval_id,
        )
        return True

    # Denied, expired, or timed out with deny.
    raise StrathonApprovalDenied(
        decision.message or f"Approval {status} for policy '{decision.policy_name}'",
        policy_id=decision.policy_id,
        policy_name=decision.policy_name,
        approval_id=approval_id,
        status=status,
    )

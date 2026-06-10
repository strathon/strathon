"""Slack integration for Strathon.

Formats notifications as Block Kit messages with interactive buttons
for approval workflows. Verifies Slack request signatures on incoming
interactions.

Event types:
  policy_blocked    → alert with policy + agent + tool details
  approval_request  → approve/deny buttons (interactive)
  approval_resolved → update original message with decision
  incident          → alert with severity, Art 73 metadata
  budget_alert      → warning with projected exhaustion
  budget_halt       → critical alert

Research: Slack Block Kit interactive messages guide, Slack request
signature verification (HMAC-SHA256 over timestamp + body), Slack
app OAuth 2.0 flow.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Callable

import httpx

logger = logging.getLogger("strathon.integrations.slack")


# ---- Block Kit Formatters ---------------------------------------------------


def format_approval_request(event: dict[str, Any], base_url: str) -> dict:
    """Format an approval request as a Slack Block Kit message with buttons."""
    approval_id = event.get("approval_id", "unknown")
    agent = event.get("agent_name", "unknown")
    tool = event.get("tool_name", "unknown")
    policy = event.get("policy_name", "unknown")
    timeout = event.get("timeout_seconds", 300)

    return {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": ":rotating_light: Approval Required",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Agent:*\n{agent}"},
                    {"type": "mrkdwn", "text": f"*Tool:*\n{tool}"},
                    {"type": "mrkdwn", "text": f"*Policy:*\n{policy}"},
                    {"type": "mrkdwn", "text": f"*Timeout:*\n{timeout}s"},
                ],
            },
            {"type": "divider"},
            {
                "type": "actions",
                "block_id": f"approval_{approval_id}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "action_id": "strathon_approve",
                        "value": json.dumps({
                            "approval_id": approval_id,
                            "base_url": base_url,
                        }),
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "style": "danger",
                        "action_id": "strathon_deny",
                        "value": json.dumps({
                            "approval_id": approval_id,
                            "base_url": base_url,
                        }),
                    },
                ],
            },
        ],
        "text": f"Approval required: {agent} wants to call {tool}",
    }


def format_incident(event: dict[str, Any]) -> dict:
    """Format an incident as a Slack Block Kit message."""
    severity = event.get("severity", "medium")
    trigger = event.get("trigger", "unknown")
    agents = event.get("affected_agents", [])
    art73 = event.get("eu_ai_act_reporting", {})

    emoji = {
        "critical": ":red_circle:",
        "high": ":large_orange_circle:",
        "medium": ":large_yellow_circle:",
    }.get(severity, ":white_circle:")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} Incident Detected — {severity.upper()}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Trigger:*\n{trigger}"},
                {"type": "mrkdwn", "text": f"*Agents:*\n{', '.join(agents) or 'all'}"},
            ],
        },
    ]
    if art73:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":eu: *EU AI Act Article 73 Reporting*\n"
                    f"Deadline: {art73.get('deadline_days', 15)} days "
                    f"({art73.get('deadline_date', 'TBD')})\n"
                    f"{art73.get('description', '')}"
                ),
            },
        })
    return {"blocks": blocks, "text": f"Incident: {trigger} ({severity})"}


def format_policy_event(event: dict[str, Any]) -> dict:
    """Format a policy match event (block/steer/throttle/alert)."""
    action = event.get("action", "unknown")
    agent = event.get("agent_name", "unknown")
    tool = event.get("tool_name", "unknown")
    policy = event.get("policy_name", "unknown")

    emoji = {
        "block": ":no_entry:",
        "steer": ":arrow_right:",
        "throttle": ":hourglass:",
        "alert": ":bell:",
    }.get(action, ":information_source:")

    return {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{emoji} *Policy {action.upper()}*\n"
                        f"Agent `{agent}` → Tool `{tool}`\n"
                        f"Policy: _{policy}_"
                    ),
                },
            },
        ],
        "text": f"Policy {action}: {agent} → {tool}",
    }


def format_budget_alert(event: dict[str, Any]) -> dict:
    """Format a budget alert or auto-halt."""
    is_halt = event.get("type") == "budget_halt"
    emoji = ":octagonal_sign:" if is_halt else ":warning:"
    title = "Budget Auto-Halt" if is_halt else "Budget Warning"

    return {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{emoji} *{title}*\n"
                        f"Budget: {event.get('budget_name', 'unknown')}\n"
                        f"Current: ${event.get('current_spend', '?')}\n"
                        f"Limit: ${event.get('limit', '?')}"
                    ),
                },
            },
        ],
        "text": f"{title}: {event.get('budget_name', 'budget')}",
    }


EVENT_FORMATTERS: dict[str, Callable[..., dict[str, Any]]] = {
    "approval_request": format_approval_request,
    "incident": format_incident,
    "policy_blocked": format_policy_event,
    "policy_steered": format_policy_event,
    "policy_throttled": format_policy_event,
    "policy_alert": format_policy_event,
    "budget_alert": format_budget_alert,
    "budget_halt": format_budget_alert,
}


# ---- Send -------------------------------------------------------------------


async def send_slack_message(
    webhook_url: str,
    payload: dict[str, Any],
    *,
    timeout: float = 10.0,
) -> bool:
    """Send a Block Kit message to a Slack webhook URL."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                webhook_url,
                json=payload,
                timeout=timeout,
            )
            if resp.status_code == 200:
                return True
            logger.warning("Slack webhook returned %d: %s", resp.status_code, resp.text)
            return False
    except Exception:
        logger.exception("Failed to send Slack message")
        return False


async def update_slack_message(
    response_url: str,
    text: str,
    *,
    replace_original: bool = True,
) -> bool:
    """Update an interactive message after button click."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                response_url,
                json={
                    "replace_original": replace_original,
                    "text": text,
                },
                timeout=10.0,
            )
            return resp.status_code == 200
    except Exception:
        logger.exception("Failed to update Slack message")
        return False


# ---- Signature Verification -------------------------------------------------


def verify_slack_signature(
    signing_secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
) -> bool:
    """Verify a Slack request signature (HMAC-SHA256).

    Slack signs requests as: v0=HMAC-SHA256(signing_secret, "v0:{timestamp}:{body}")
    Reject if timestamp is >5 min old (replay protection).
    """
    # Replay protection.
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False
    if abs(time.time() - ts) > 300:
        return False

    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}"
    expected = (
        "v0="
        + hmac.new(
            signing_secret.encode(),
            sig_basestring.encode(),
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(expected, signature)

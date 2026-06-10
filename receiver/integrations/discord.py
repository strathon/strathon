"""Discord integration for Strathon.

Formats notifications as Discord rich embeds with interactive
buttons for approval workflows.

Research: Discord webhook embed format, Discord interactions
API, Ed25519 signature verification for Discord interactions.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

logger = logging.getLogger("strathon.integrations.discord")


# ---- Colors -----------------------------------------------------------------

COLOR_RED = 0xED4245
COLOR_ORANGE = 0xE67E22
COLOR_YELLOW = 0xFEE75C
COLOR_GREEN = 0x57F287
COLOR_BLUE = 0x5865F2


# ---- Embed Formatters -------------------------------------------------------


def format_approval_request(event: dict[str, Any], base_url: str) -> dict:
    """Format an approval request as a Discord embed with buttons."""
    approval_id = event.get("approval_id", "unknown")
    agent = event.get("agent_name", "unknown")
    tool = event.get("tool_name", "unknown")
    policy = event.get("policy_name", "unknown")
    timeout = event.get("timeout_seconds", 300)

    return {
        "embeds": [
            {
                "title": ":rotating_light: Approval Required",
                "color": COLOR_ORANGE,
                "fields": [
                    {"name": "Agent", "value": agent, "inline": True},
                    {"name": "Tool", "value": tool, "inline": True},
                    {"name": "Policy", "value": policy, "inline": True},
                    {"name": "Timeout", "value": f"{timeout}s", "inline": True},
                    {
                        "name": "Actions",
                        "value": (
                            f"[Approve]({base_url}/v1/approvals/{approval_id}/approve) | "
                            f"[Deny]({base_url}/v1/approvals/{approval_id}/deny)"
                        ),
                    },
                ],
                "footer": {"text": f"Approval ID: {approval_id}"},
            },
        ],
        "components": [
            {
                "type": 1,  # Action Row
                "components": [
                    {
                        "type": 2,  # Button
                        "style": 3,  # Success (green)
                        "label": "Approve",
                        "custom_id": f"strathon_approve:{approval_id}",
                    },
                    {
                        "type": 2,
                        "style": 4,  # Danger (red)
                        "label": "Deny",
                        "custom_id": f"strathon_deny:{approval_id}",
                    },
                ],
            },
        ],
    }


def format_incident(event: dict[str, Any]) -> dict:
    """Format an incident as a Discord embed."""
    severity = event.get("severity", "medium")
    trigger = event.get("trigger", "unknown")
    agents = event.get("affected_agents", [])
    art73 = event.get("eu_ai_act_reporting", {})

    color = {
        "critical": COLOR_RED,
        "high": COLOR_ORANGE,
        "medium": COLOR_YELLOW,
    }.get(severity, COLOR_BLUE)

    fields = [
        {"name": "Trigger", "value": trigger, "inline": True},
        {"name": "Severity", "value": severity.upper(), "inline": True},
        {"name": "Agents", "value": ", ".join(agents) or "all", "inline": True},
    ]
    if art73:
        fields.append({
            "name": ":flag_eu: EU AI Act Article 73",
            "value": (
                f"Deadline: {art73.get('deadline_days', 15)} days "
                f"({art73.get('deadline_date', 'TBD')})"
            ),
        })

    return {
        "embeds": [
            {
                "title": f"Incident — {severity.upper()}",
                "color": color,
                "fields": fields,
            },
        ],
    }


def format_policy_event(event: dict[str, Any]) -> dict:
    """Format a policy match event."""
    action = event.get("action", "unknown")
    agent = event.get("agent_name", "unknown")
    tool = event.get("tool_name", "unknown")
    policy = event.get("policy_name", "unknown")

    color = {
        "block": COLOR_RED,
        "steer": COLOR_ORANGE,
        "throttle": COLOR_YELLOW,
        "alert": COLOR_BLUE,
    }.get(action, COLOR_BLUE)

    return {
        "embeds": [
            {
                "title": f"Policy {action.upper()}",
                "color": color,
                "fields": [
                    {"name": "Agent", "value": agent, "inline": True},
                    {"name": "Tool", "value": tool, "inline": True},
                    {"name": "Policy", "value": policy, "inline": True},
                ],
            },
        ],
    }


def format_budget_alert(event: dict[str, Any]) -> dict:
    """Format a budget alert or halt."""
    is_halt = event.get("type") == "budget_halt"
    color = COLOR_RED if is_halt else COLOR_YELLOW
    title = "Budget Auto-Halt" if is_halt else "Budget Warning"

    return {
        "embeds": [
            {
                "title": title,
                "color": color,
                "fields": [
                    {"name": "Budget", "value": event.get("budget_name", "unknown"), "inline": True},
                    {"name": "Current", "value": f"${event.get('current_spend', '?')}", "inline": True},
                    {"name": "Limit", "value": f"${event.get('limit', '?')}", "inline": True},
                ],
            },
        ],
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


async def send_discord_message(
    webhook_url: str,
    payload: dict[str, Any],
    *,
    timeout: float = 10.0,
) -> bool:
    """Send an embed message to a Discord webhook URL."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                webhook_url,
                json=payload,
                timeout=timeout,
            )
            if resp.status_code in (200, 204):
                return True
            logger.warning("Discord webhook returned %d: %s", resp.status_code, resp.text)
            return False
    except Exception:
        logger.exception("Failed to send Discord message")
        return False

"""Dispatch notifications to configured channels.

Routes events to Slack, Discord, GitHub, or generic webhooks
based on the notification_channels configuration for a project.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from integrations import slack, discord

logger = logging.getLogger("strathon.integrations.dispatcher")


async def dispatch_event(
    session: AsyncSession,
    project_id: UUID,
    event_type: str,
    event_data: dict[str, Any],
    *,
    base_url: str = "http://localhost:4318",
) -> int:
    """Send an event to all matching notification channels.

    Returns the number of channels notified.
    """
    result = await session.execute(text(
        "SELECT * FROM notification_channels "
        "WHERE project_id = :pid AND enabled = TRUE"
    ), {"pid": project_id})
    channels = result.mappings().all()

    sent = 0
    for ch in channels:
        # Check event filter.
        events_filter = ch.get("events") or []
        if events_filter and event_type not in events_filter:
            continue

        channel_type = ch["channel_type"]
        config = ch.get("config") or {}

        try:
            if channel_type == "slack":
                ok = await _send_slack(config, event_type, event_data, base_url)
            elif channel_type == "discord":
                ok = await _send_discord(config, event_type, event_data, base_url)
            elif channel_type == "webhook":
                ok = await _send_webhook(config, event_type, event_data)
            elif channel_type == "github":
                ok = await _send_github(config, event_type, event_data)
            else:
                logger.warning("Unknown channel type: %s", channel_type)
                continue

            if ok:
                sent += 1
        except Exception:
            logger.exception(
                "Failed to dispatch %s to channel %s (%s)",
                event_type, ch["id"], channel_type,
            )

    return sent


async def _send_slack(
    config: dict, event_type: str, event_data: dict, base_url: str,
) -> bool:
    webhook_url = config.get("webhook_url")
    if not webhook_url:
        return False

    formatter = slack.EVENT_FORMATTERS.get(event_type)
    if formatter is None:
        # Default: send raw JSON as text.
        payload = {"text": f"[Strathon] {event_type}: {event_data}"}
    elif event_type == "approval_request":
        payload = formatter(event_data, base_url)
    else:
        payload = formatter(event_data)

    return await slack.send_slack_message(webhook_url, payload)


async def _send_discord(
    config: dict, event_type: str, event_data: dict, base_url: str,
) -> bool:
    webhook_url = config.get("webhook_url")
    if not webhook_url:
        return False

    formatter = discord.EVENT_FORMATTERS.get(event_type)
    if formatter is None:
        payload = {"content": f"[Strathon] {event_type}: {event_data}"}
    elif event_type == "approval_request":
        payload = formatter(event_data, base_url)
    else:
        payload = formatter(event_data)

    return await discord.send_discord_message(webhook_url, payload)


async def _send_webhook(
    config: dict, event_type: str, event_data: dict,
) -> bool:
    """Generic webhook — raw JSON POST."""
    import httpx
    url = config.get("url")
    if not url:
        return False

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                json={"event_type": event_type, **event_data},
                timeout=10.0,
            )
            return resp.status_code < 400
    except Exception:
        logger.exception("Generic webhook failed")
        return False


async def _send_github(
    config: dict, event_type: str, event_data: dict,
) -> bool:
    """Create a GitHub issue for incidents."""
    import httpx

    token = config.get("token")
    repo = config.get("repo")  # "owner/repo"
    if not token or not repo:
        return False

    # Issues are for events worth a durable, assignable record. Incidents,
    # budget halts, SDK integrity violations, and behavioral drift qualify;
    # high-frequency signals like missed heartbeats do not (they would file
    # an issue every time an agent is simply not running).
    issue_events = (
        "incident", "budget_halt", "sdk_integrity_violation", "behavioral_drift",
    )
    if event_type not in issue_events:
        return True

    severity = event_data.get("severity", "medium")
    # Incidents carry a "trigger"; other events carry a human-readable
    # "message". Use whichever is present for the issue title and body.
    summary = event_data.get("trigger") or event_data.get("message") or event_type
    labels = [
        f"strathon-{event_type.replace('_', '-')}",
        f"severity-{severity}",
    ]

    body_parts = [
        f"**Event:** {event_type}",
        f"**Severity:** {severity}",
    ]
    if event_data.get("agent_name"):
        body_parts.append(f"**Agent:** {event_data['agent_name']}")
    if event_data.get("trigger"):
        body_parts.append(f"**Trigger:** {event_data['trigger']}")
    if event_data.get("message"):
        body_parts.append(f"\n{event_data['message']}")
    if event_data.get("affected_agents"):
        body_parts.append(
            f"**Agents:** {', '.join(event_data['affected_agents'])}"
        )
    art73 = event_data.get("eu_ai_act_reporting")
    if art73:
        body_parts.append(
            f"\n**EU AI Act Article 73 Reporting**\n"
            f"Deadline: {art73.get('deadline_days', 15)} days "
            f"({art73.get('deadline_date', 'TBD')})"
        )
    if event_data.get("recommended_actions"):
        body_parts.append(
            "\n**Recommended Actions:**\n"
            + "\n".join(f"- {a}" for a in event_data["recommended_actions"])
        )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.github.com/repos/{repo}/issues",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                json={
                    "title": f"[Strathon] {summary} ({severity})",
                    "body": "\n".join(body_parts),
                    "labels": labels,
                },
                timeout=15.0,
            )
            return resp.status_code in (200, 201)
    except Exception:
        logger.exception("GitHub issue creation failed")
        return False

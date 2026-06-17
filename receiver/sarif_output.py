"""SARIF v2.1.0 output for GitHub Code Scanning integration.

Converts Strathon policy violations and credential findings into
SARIF format that integrates with GitHub's Security tab.

Usage:
    GET /v1/compliance/sarif — generates SARIF report from recent
    policy violations and credential scan findings.

Research: SARIF v2.1.0 specification (OASIS), GitHub Code Scanning
SARIF upload format.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json"

TOOL_NAME = "strathon"
TOOL_VERSION = "1.2.1"
TOOL_URI = "https://getstrathon.com"


def _severity_to_sarif_level(severity: str) -> str:
    """Map Strathon severity to SARIF level."""
    return {
        "critical": "error",
        "high": "error",
        "medium": "warning",
        "low": "note",
    }.get(severity, "warning")


def policy_violation_to_result(violation: dict[str, Any]) -> dict[str, Any]:
    """Convert a policy violation event to a SARIF result."""
    return {
        "ruleId": f"strathon/{violation.get('policy_name', 'unknown')}",
        "level": _severity_to_sarif_level(violation.get("severity", "medium")),
        "message": {
            "text": (
                f"Policy '{violation.get('policy_name', '?')}' "
                f"triggered action '{violation.get('action', '?')}' "
                f"on agent '{violation.get('agent_name', '?')}' "
                f"calling tool '{violation.get('tool_name', '?')}'."
            ),
        },
        "properties": {
            "strathon.policy_id": violation.get("policy_id", ""),
            "strathon.agent_name": violation.get("agent_name", ""),
            "strathon.tool_name": violation.get("tool_name", ""),
            "strathon.action": violation.get("action", ""),
            "strathon.trace_id": violation.get("trace_id", ""),
            "strathon.timestamp": violation.get("timestamp", ""),
        },
    }


def credential_finding_to_result(finding: dict[str, Any]) -> dict[str, Any]:
    """Convert a credential scan finding to a SARIF result."""
    return {
        "ruleId": f"strathon/credential/{finding.get('pattern_id', 'unknown')}",
        "level": _severity_to_sarif_level(finding.get("severity", "high")),
        "message": {
            "text": (
                f"Detected {finding.get('pattern_name', 'credential')} "
                f"in agent output. Category: {finding.get('category', 'unknown')}."
            ),
        },
        "properties": {
            "strathon.pattern_id": finding.get("pattern_id", ""),
            "strathon.category": finding.get("category", ""),
            "strathon.severity": finding.get("severity", ""),
        },
    }


def incident_to_result(incident: dict[str, Any]) -> dict[str, Any]:
    """Convert an incident detection event to a SARIF result."""
    severity = incident.get("severity", "medium")
    art73 = incident.get("eu_ai_act_reporting", {})

    message = (
        f"Incident detected: {incident.get('trigger', 'unknown')} "
        f"(severity: {severity})."
    )
    if art73:
        message += (
            f" EU AI Act Article 73 reporting deadline: "
            f"{art73.get('deadline_days', 15)} days."
        )

    return {
        "ruleId": f"strathon/incident/{incident.get('trigger', 'unknown')}",
        "level": _severity_to_sarif_level(severity),
        "message": {"text": message},
        "properties": {
            "strathon.incident_id": incident.get("incident_id", ""),
            "strathon.trigger": incident.get("trigger", ""),
            "strathon.affected_agents": incident.get("affected_agents", []),
            "strathon.eu_ai_act_deadline_days": art73.get("deadline_days"),
        },
    }


def generate_sarif(
    violations: list[dict] | None = None,
    credential_findings: list[dict] | None = None,
    incidents: list[dict] | None = None,
) -> dict[str, Any]:
    """Generate a complete SARIF v2.1.0 document."""
    results = []

    # Build rule index from results.
    rules_map: dict[str, dict] = {}

    for v in (violations or []):
        result = policy_violation_to_result(v)
        results.append(result)
        rule_id = result["ruleId"]
        if rule_id not in rules_map:
            rules_map[rule_id] = {
                "id": rule_id,
                "name": v.get("policy_name", "unknown"),
                "shortDescription": {
                    "text": f"Strathon policy: {v.get('policy_name', '?')}",
                },
                "properties": {
                    "category": "policy-violation",
                    "owasp": v.get("owasp_ref", ""),
                },
            }

    for f in (credential_findings or []):
        result = credential_finding_to_result(f)
        results.append(result)
        rule_id = result["ruleId"]
        if rule_id not in rules_map:
            rules_map[rule_id] = {
                "id": rule_id,
                "name": f.get("pattern_name", "unknown"),
                "shortDescription": {
                    "text": f"Credential detection: {f.get('pattern_name', '?')}",
                },
                "properties": {"category": "credential-leak"},
            }

    for inc in (incidents or []):
        result = incident_to_result(inc)
        results.append(result)
        rule_id = result["ruleId"]
        if rule_id not in rules_map:
            rules_map[rule_id] = {
                "id": rule_id,
                "name": inc.get("trigger", "unknown"),
                "shortDescription": {
                    "text": f"Incident: {inc.get('trigger', '?')}",
                },
                "properties": {"category": "incident"},
            }

    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": TOOL_NAME,
                        "version": TOOL_VERSION,
                        "informationUri": TOOL_URI,
                        "rules": list(rules_map.values()),
                    },
                },
                "results": results,
                "invocations": [
                    {
                        "executionSuccessful": True,
                        "endTimeUtc": datetime.now(timezone.utc).isoformat(),
                    },
                ],
            },
        ],
    }

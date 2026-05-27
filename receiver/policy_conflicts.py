"""Policy conflict detection.

Analyzes enabled policies for a project and reports potential
conflicts. Three conflict types from firewall policy analysis
literature (Al-Shaer & Hamed 2005, Cuppens et al. 2007):

  contradiction: same match condition, different actions
  redundancy:    same match condition, same action (wasted eval)
  shadowing:     higher-priority policy fully covers a lower one

Since CEL expressions are opaque strings (no theorem prover), we
detect conflicts via:
  1. Exact match_expression equality (catches copy-paste errors)
  2. Tool name extraction from common CEL patterns
     (attrs["gen_ai.tool.name"] == "X")

This is heuristic, not complete. It won't catch semantic overlaps
like 'x > 5' vs 'x > 3'. But it catches the most common operator
mistakes at zero runtime cost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class Conflict:
    type: str  # contradiction, redundancy, shadowing
    policy_a_id: str
    policy_a_name: str
    policy_b_id: str
    policy_b_name: str
    reason: str


_TOOL_NAME_RE = re.compile(
    r'attrs\["gen_ai\.tool\.name"\]\s*==\s*"([^"]+)"'
)


def _extract_tool_names(expr: str) -> set[str]:
    """Extract tool names from CEL expressions."""
    return set(_TOOL_NAME_RE.findall(expr))


def detect_conflicts(policies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Analyze policies for conflicts.

    Takes a list of policy dicts (from the DB). Returns a list of
    conflict descriptions.
    """
    enabled = [p for p in policies if p.get("enabled", True)]
    conflicts: list[Conflict] = []

    for i, a in enumerate(enabled):
        for b in enabled[i + 1:]:
            # Exact expression match.
            if a["match_expression"] == b["match_expression"]:
                if a["action"] == b["action"]:
                    conflicts.append(Conflict(
                        type="redundancy",
                        policy_a_id=str(a["id"]),
                        policy_a_name=a["name"],
                        policy_b_id=str(b["id"]),
                        policy_b_name=b["name"],
                        reason=(
                            f"identical match_expression and action "
                            f"({a['action']}); one is redundant"
                        ),
                    ))
                else:
                    conflicts.append(Conflict(
                        type="contradiction",
                        policy_a_id=str(a["id"]),
                        policy_a_name=a["name"],
                        policy_b_id=str(b["id"]),
                        policy_b_name=b["name"],
                        reason=(
                            f"identical match_expression but different "
                            f"actions ({a['action']} vs {b['action']})"
                        ),
                    ))
                continue

            # Tool name overlap with different actions.
            tools_a = _extract_tool_names(a["match_expression"])
            tools_b = _extract_tool_names(b["match_expression"])
            overlap = tools_a & tools_b
            if overlap and a["action"] != b["action"]:
                # Check if one is block/allow and the other is opposite.
                actions = {a["action"], b["action"]}
                if actions & {"block", "allow"}:
                    conflicts.append(Conflict(
                        type="contradiction",
                        policy_a_id=str(a["id"]),
                        policy_a_name=a["name"],
                        policy_b_id=str(b["id"]),
                        policy_b_name=b["name"],
                        reason=(
                            f"both match tool(s) {sorted(overlap)} but "
                            f"with conflicting actions "
                            f"({a['action']} vs {b['action']})"
                        ),
                    ))

    return [
        {
            "type": c.type,
            "policy_a": {"id": c.policy_a_id, "name": c.policy_a_name},
            "policy_b": {"id": c.policy_b_id, "name": c.policy_b_name},
            "reason": c.reason,
        }
        for c in conflicts
    ]

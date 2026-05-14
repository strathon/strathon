"""Policy data types shared between server and SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Policy:
    """A compiled, validated policy ready for evaluation.

    match_expression is a CEL expression string evaluated against a span
    context with shape ``{"name": str, "attrs": dict[str, Any]}``.

    See ``strathon.policy.expression`` for syntax and examples.
    """

    id: str
    project_id: str
    name: str
    match_expression: str
    action: str  # 'log' | 'alert' | 'block' | 'steer'
    action_config: Dict[str, Any] = field(default_factory=dict)
    applies_to: List[str] = field(default_factory=list)
    enabled: bool = True
    priority: int = 0
    description: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "name": self.name,
            "description": self.description,
            "match_expression": self.match_expression,
            "action": self.action,
            "action_config": self.action_config,
            "applies_to": self.applies_to,
            "enabled": self.enabled,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Policy":
        return cls(
            id=str(data["id"]),
            project_id=str(data["project_id"]),
            name=data["name"],
            description=data.get("description"),
            match_expression=data["match_expression"],
            action=data["action"],
            action_config=data.get("action_config") or {},
            applies_to=list(data.get("applies_to") or []),
            enabled=data.get("enabled", True),
            priority=data.get("priority", 0),
        )


@dataclass(frozen=True)
class PolicyDecision:
    """The result of evaluating active policies against an action.

    `action` is one of 'allow', 'block', 'steer'. 'log' and 'alert' don't
    affect control flow and are not returned as decisions; they are applied
    as side effects by the server.

    When action == 'steer', `replacement` holds the corrective string the
    SDK should return in place of the real tool/LLM output.

    When action == 'block', `message` holds the human-readable reason that
    will be attached to the raised StrathonPolicyBlocked exception.
    """

    action: str  # 'allow' | 'block' | 'steer'
    policy_id: Optional[str] = None
    policy_name: Optional[str] = None
    message: Optional[str] = None
    replacement: Optional[str] = None

    @property
    def is_allow(self) -> bool:
        return self.action == "allow"

    @property
    def is_block(self) -> bool:
        return self.action == "block"

    @property
    def is_steer(self) -> bool:
        return self.action == "steer"


# Module-level singleton for the most common case
ALLOW = PolicyDecision(action="allow")


class StrathonPolicyBlocked(Exception):
    """Raised by Strathon when a runtime intervention policy blocks an action."""

    def __init__(
        self,
        message: str,
        policy_id: Optional[str] = None,
        policy_name: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.policy_id = policy_id
        self.policy_name = policy_name

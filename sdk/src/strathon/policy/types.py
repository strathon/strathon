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
    # Shadow policies are evaluated and recorded server-side but must never
    # enforce. The SDK previously dropped this field on parse, which made a
    # shadow block policy block for real in-process — the inverse of the
    # documented "test without enforcing" contract.
    shadow: bool = False

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
            "shadow": self.shadow,
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
            shadow=bool(data.get("shadow", False)),
        )


@dataclass(frozen=True)
class PolicyDecision:
    """The result of evaluating active policies against an action.

    `action` is one of 'allow', 'block', 'steer', 'throttle'. 'log' and
    'alert' don't affect control flow and are not returned as decisions;
    they are applied as side effects by the server.

    When action == 'steer', `replacement` holds the corrective string the
    SDK should return in place of the real tool/LLM output.

    When action == 'block', `message` holds the human-readable reason that
    will be attached to the raised StrathonPolicyBlocked exception.

    When action == 'throttle', the call exceeded the policy's rate cap.
    `message` carries a human-readable reason. `retry_after_seconds`
    estimates how long until the bucket has at least one token again,
    based on the configured refill rate.
    """

    action: str  # 'allow' | 'block' | 'steer' | 'throttle' | 'require_approval'
    policy_id: Optional[str] = None
    policy_name: Optional[str] = None
    message: Optional[str] = None
    replacement: Optional[str] = None
    retry_after_seconds: Optional[float] = None
    approval_id: Optional[str] = None
    timeout_seconds: Optional[int] = None

    @property
    def is_allow(self) -> bool:
        return self.action == "allow"

    @property
    def is_block(self) -> bool:
        return self.action == "block"

    @property
    def is_steer(self) -> bool:
        return self.action == "steer"

    @property
    def is_throttle(self) -> bool:
        return self.action == "throttle"

    @property
    def is_require_approval(self) -> bool:
        return self.action == "require_approval"


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


class StrathonPolicyThrottled(StrathonPolicyBlocked):
    """Raised when a 'throttle' policy refused this specific call because
    its rate limit was already exhausted.

    Subclass of :class:`StrathonPolicyBlocked` deliberately: code that
    catches ``StrathonPolicyBlocked`` to handle policy refusal in
    aggregate continues to work and treats throttling as a kind of
    block. Code that wants to distinguish — e.g. to sleep
    ``retry_after_seconds`` and retry the tool rather than escalate to
    the user — can catch ``StrathonPolicyThrottled`` specifically.

    The semantic is distinct: ``block`` says "this call would have
    violated the rule"; ``throttle`` says "the rule allows this call,
    just not at this rate." Operators handling the two cases
    differently — alert vs backoff-and-retry — need the distinction.
    """

    def __init__(
        self,
        message: str,
        policy_id: Optional[str] = None,
        policy_name: Optional[str] = None,
        retry_after_seconds: Optional[float] = None,
    ) -> None:
        super().__init__(message, policy_id=policy_id, policy_name=policy_name)
        self.retry_after_seconds = retry_after_seconds


class StrathonApprovalDenied(StrathonPolicyBlocked):
    """Raised when a require_approval policy decision is denied or expired.

    The tool call was held pending human approval, and the operator
    either explicitly denied it or the approval timed out.

    ``approval_id`` identifies the approval record on the receiver.
    ``status`` is 'denied' or 'expired'.
    """

    def __init__(
        self,
        message: str,
        policy_id: Optional[str] = None,
        policy_name: Optional[str] = None,
        approval_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> None:
        super().__init__(message, policy_id=policy_id, policy_name=policy_name)
        self.approval_id = approval_id
        self.status = status or "denied"


# ---- Halt decisions ----------------------------------------------------
#
# Halts are operator-imposed kill-switches that live on the server in
# the halt_state table and are pulled by the SDK on a fast poll. They
# are a separate concept from policy match decisions: a halt is "stop
# this agent, no matter what it tries to do," not "this specific
# action violates this rule." Hence a separate decision type and a
# separate exception. Frameworks check halt FIRST at the per-tool
# boundary; if a halt is active the policy code never runs (no point
# evaluating match expressions when the agent is supposed to be off).


@dataclass(frozen=True)
class HaltDecision:
    """The result of consulting the halt cache for a given call.

    action is 'allow' or 'halt'. On 'halt', the SDK raises
    StrathonHaltExceeded at the tool boundary. halt_id is the row id
    in the receiver's halt_state table; scope/scope_value identify
    which halt fired (useful for the operator looking at the
    exception in their logs).
    """

    action: str  # 'allow' | 'halt'
    halt_id: Optional[int] = None
    scope: Optional[str] = None         # 'agent' | 'project'
    scope_value: Optional[str] = None   # agent_id, or None for project-scope
    reason: Optional[str] = None
    state: Optional[str] = None         # 'paused' | 'halted'

    @property
    def is_allow(self) -> bool:
        return self.action == "allow"

    @property
    def is_halt(self) -> bool:
        return self.action == "halt"


# Module-level singleton — every allow returns this.
ALLOW_HALT = HaltDecision(action="allow")


class StrathonHaltExceeded(Exception):
    """Raised when an operator-imposed halt is active for the calling agent.

    Distinct from StrathonPolicyBlocked because the semantics differ:
    a block says "this specific action would violate a policy", while
    a halt says "this agent has been stopped by an operator regardless
    of what it tries to do." Callers handling the two cases
    differently (e.g. retry-able-vs-not, alert-vs-no-alert) need the
    distinction.
    """

    def __init__(
        self,
        message: str,
        halt_id: Optional[int] = None,
        scope: Optional[str] = None,
        scope_value: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.halt_id = halt_id
        self.scope = scope
        self.scope_value = scope_value
        self.reason = reason

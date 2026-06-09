"""Pre-call hooks for runtime intervention.

Note: The primary enforcement mechanism is the policy evaluator
(sdk/policy/enforcer.py) and approval workflow (sdk/policy/approval.py).
This hook provides an additional intervention point for dashboard-driven
halt/pause commands.
"""

from enum import Enum


class InterventionState(str, Enum):
    PROCEED = "proceed"
    PAUSE = "pause"
    HALT = "halt"


class InterventionHook:
    """Pre-call hook that checks halt state before each LLM call."""

    def __init__(self, client):
        self.client = client

    def before_call(self, agent_id: str, trace_id: str) -> InterventionState:
        """Check halt state via the SDK's halt enforcer.

        Mirrors the span-context shape every other surface uses: the agent id
        goes in ``attrs`` under ``strathon.agent.id`` (check_halt reads
        ``span_context["attrs"]``), and the result is a HaltDecision whose
        ``is_halt`` flag indicates an active operator halt.
        """
        if getattr(self.client, "_halt_enforcer", None) is not None:
            halt_result = self.client.check_halt(
                {"attrs": {"strathon.agent.id": agent_id}}
            )
            if halt_result is not None and halt_result.is_halt:
                return InterventionState.HALT
        return InterventionState.PROCEED

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
        """Check halt state via the SDK's halt enforcer."""
        if self.client.halt_enforcer is not None:
            halt_result = self.client.check_halt({"agent_id": agent_id})
            if halt_result and halt_result.halted:
                return InterventionState.HALT
        return InterventionState.PROCEED

"""Pre-call hooks for runtime intervention.

These hooks check the Strathon receiver before each LLM call to determine
whether the agent should proceed, pause, or halt.

Note: The primary enforcement mechanism is the policy evaluator
(sdk/policy/enforcer.py) and approval workflow (sdk/policy/approval.py).
This hook provides an additional intervention point for dashboard-driven
halt/pause commands.
"""

from enum import Enum


class InterventionState(str, Enum):
    """Possible intervention states returned by the receiver."""

    PROCEED = "proceed"
    PAUSE = "pause"
    HALT = "halt"


class InterventionHook:
    """
    Pre-call hook that checks intervention state before each LLM call.

    Allows the Strathon dashboard to pause, resume, or halt running agents
    in real time. Halt state persists across process restarts via the
    server-side write-ahead log.
    """

    def __init__(self, client):
        self.client = client

    def before_call(self, agent_id: str, trace_id: str) -> InterventionState:
        """
        Called before each LLM call. Checks halt state via the SDK's
        built-in halt enforcer (which polls /v1/halts periodically).

        For approval-based intervention, see sdk/policy/approval.py
        which handles the require_approval action with full poll loop.
        """
        if self.client.halt_enforcer is not None:
            halt_result = self.client.check_halt({"agent_id": agent_id})
            if halt_result and halt_result.halted:
                return InterventionState.HALT
        return InterventionState.PROCEED

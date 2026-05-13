"""Pre-call hooks for runtime intervention.

These hooks check the Strathon receiver before each LLM call to determine
whether the agent should proceed, pause, or halt.
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

    Args:
        client: Strathon Client instance.
    """

    def __init__(self, client):
        self.client = client

    def before_call(self, agent_id: str, trace_id: str) -> InterventionState:
        """
        Called before each LLM call. Queries intervention sync API.

        Args:
            agent_id: Identifier for the agent making the call.
            trace_id: Current trace ID.

        Returns:
            InterventionState.PROCEED to continue normally.
            InterventionState.PAUSE to wait until resumed.
            InterventionState.HALT to abort the agent execution.
        """
        # TODO: GET {endpoint}/v1/intervention/sync?agent_id=...&trace_id=...
        # TODO: poll with backoff if PAUSE returned
        # TODO: raise InterventionError on HALT and persist locally
        return InterventionState.PROCEED

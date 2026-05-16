"""Strathon SDK exceptions."""


class StrathonError(Exception):
    """Base exception for all Strathon SDK errors."""


class AuthenticationError(StrathonError):
    """Raised when API key is missing or invalid."""


class ExportError(StrathonError):
    """Raised when trace export to the Strathon receiver fails."""


class InterventionError(StrathonError):
    """Raised when intervention sync API call fails."""


class StrathonReceiverUnreachable(StrathonError):
    """Raised at a tool boundary when fail-closed mode is on AND the SDK's
    cached intervention state is older than the configured staleness
    threshold.

    The semantic is "the SDK cannot confirm with the receiver that the
    last-known policy and halt state is still current, and the operator
    has explicitly chosen safety-over-availability." Distinct from
    ``StrathonHaltExceeded`` (an operator deliberately stopped this
    agent) and ``StrathonPolicyBlocked`` (a specific match fired):
    callers handling fail-closed differently — e.g. by paging an
    on-call engineer rather than retrying — need the distinction.

    The error message reports which subsystem detected the staleness
    (halt cache or policy cache) and how stale the state was at the
    moment of the call, so logs make the cause obvious.

    Only raised when ``Client(fail_closed=True)``; the default
    fail-open behavior never raises this exception.
    """

    def __init__(
        self,
        message: str,
        *,
        subsystem: str,
        staleness_seconds: float,
        max_staleness_seconds: float,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.subsystem = subsystem
        self.staleness_seconds = staleness_seconds
        self.max_staleness_seconds = max_staleness_seconds

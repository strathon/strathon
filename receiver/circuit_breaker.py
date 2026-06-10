"""Circuit breaker for per-agent and per-tool failure tracking.

Three states: CLOSED (normal) → OPEN (tripped) → HALF_OPEN (probing
recovery). Trips after N errors in M minutes. Self-heals after a
configurable cooldown.

What OPEN means: spans ingested for that agent/tool are annotated with
``strathon.circuit_breaker.state`` / ``strathon.circuit_breaker.entity``,
and the breaker is visible via GET /v1/circuit-breakers and the dashboard.
The breaker does NOT itself prevent the agent's calls — enforcement that
stops calls is the SDK policy engine and halts. To turn a trip into a hard
stop, pair it with a halt (manual or automated on the alert) or a policy.

Different from halts: halts are kill switches that the SDK enforces at the
tool boundary. Circuit breakers are automatic, self-recovering failure
*signals* with hysteresis — they tell you (and your automation) that an
agent or tool is repeatedly failing.

Execution model: this is request-path, NOT a background loop. State lives
in-memory and is updated synchronously where calls happen — span ingest
(api/traces.py) records outcomes and consults check_circuit, and the
security-tools API lists/resets breakers. There is intentionally no
asyncio task to schedule in main.py; do not add one.

Research:
Martin Fowler circuit breaker pattern, Netflix Hystrix (retired but
pattern is canonical), resilience4j.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("strathon.circuit_breaker")


class State(str, Enum):
    CLOSED = "closed"        # Normal operation.
    OPEN = "open"            # All calls blocked.
    HALF_OPEN = "half_open"  # Testing with one call.


# Configurable defaults.
ERROR_THRESHOLD = int(os.environ.get("STRATHON_CB_ERROR_THRESHOLD", "10"))
WINDOW_SECONDS = int(os.environ.get("STRATHON_CB_WINDOW_SECONDS", "300"))
COOLDOWN_SECONDS = int(os.environ.get("STRATHON_CB_COOLDOWN_SECONDS", "60"))
HALF_OPEN_MAX = int(os.environ.get("STRATHON_CB_HALF_OPEN_MAX", "3"))


@dataclass
class CircuitBreaker:
    """Per-entity circuit breaker (one per agent or per tool)."""

    entity_id: str  # agent_name or tool_name
    entity_type: str  # "agent" or "tool"
    state: State = State.CLOSED
    error_timestamps: list[float] = field(default_factory=list)
    opened_at: float = 0.0
    half_open_successes: int = 0
    half_open_failures: int = 0
    total_trips: int = 0

    def record_success(self) -> None:
        """Record a successful call."""
        if self.state == State.HALF_OPEN:
            self.half_open_successes += 1
            if self.half_open_successes >= HALF_OPEN_MAX:
                self._close()
                logger.info(
                    "Circuit breaker CLOSED (recovered): %s %s",
                    self.entity_type, self.entity_id,
                )

    def record_error(self) -> None:
        """Record a failed call. May trip the breaker."""
        now = time.monotonic()

        if self.state == State.HALF_OPEN:
            self.half_open_failures += 1
            self._open(now)
            logger.warning(
                "Circuit breaker RE-OPENED (half-open failure): %s %s",
                self.entity_type, self.entity_id,
            )
            return

        if self.state == State.OPEN:
            return  # Already open.

        # CLOSED: track errors in window.
        self.error_timestamps.append(now)
        cutoff = now - WINDOW_SECONDS
        self.error_timestamps = [t for t in self.error_timestamps if t > cutoff]

        if len(self.error_timestamps) >= ERROR_THRESHOLD:
            self._open(now)
            logger.warning(
                "Circuit breaker OPENED: %s %s (%d errors in %ds)",
                self.entity_type, self.entity_id,
                len(self.error_timestamps), WINDOW_SECONDS,
            )

    def should_block(self) -> bool:
        """Check if the circuit breaker should block the call."""
        now = time.monotonic()

        if self.state == State.CLOSED:
            return False

        if self.state == State.OPEN:
            if now - self.opened_at >= COOLDOWN_SECONDS:
                self._half_open()
                logger.info(
                    "Circuit breaker HALF-OPEN: %s %s (testing)",
                    self.entity_type, self.entity_id,
                )
                return False  # Allow the test call.
            return True  # Still in cooldown.

        # HALF_OPEN: allow (testing).
        return False

    def _open(self, now: float) -> None:
        self.state = State.OPEN
        self.opened_at = now
        self.total_trips += 1
        self.half_open_successes = 0
        self.half_open_failures = 0

    def _half_open(self) -> None:
        self.state = State.HALF_OPEN
        self.half_open_successes = 0
        self.half_open_failures = 0

    def _close(self) -> None:
        self.state = State.CLOSED
        self.error_timestamps.clear()
        self.half_open_successes = 0
        self.half_open_failures = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "state": self.state.value,
            "errors_in_window": len(self.error_timestamps),
            "total_trips": self.total_trips,
            "error_threshold": ERROR_THRESHOLD,
            "window_seconds": WINDOW_SECONDS,
            "cooldown_seconds": COOLDOWN_SECONDS,
        }


# ---- Global registry ---------------------------------------------------------

_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(entity_id: str, entity_type: str = "agent") -> CircuitBreaker:
    """Get or create a circuit breaker for an entity."""
    key = f"{entity_type}:{entity_id}"
    if key not in _breakers:
        _breakers[key] = CircuitBreaker(entity_id=entity_id, entity_type=entity_type)
    return _breakers[key]


def check_circuit(agent_name: str, tool_name: str | None = None) -> dict | None:
    """Check circuit breakers for an agent and optionally a tool.

    Returns None if allowed, or a dict with block details if blocked.
    """
    agent_cb = get_breaker(agent_name, "agent")
    if agent_cb.should_block():
        return {
            "blocked_by": "circuit_breaker",
            "entity_type": "agent",
            "entity_id": agent_name,
            "state": agent_cb.state.value,
            "message": f"Agent '{agent_name}' circuit breaker is open (too many errors)",
        }

    if tool_name:
        tool_cb = get_breaker(tool_name, "tool")
        if tool_cb.should_block():
            return {
                "blocked_by": "circuit_breaker",
                "entity_type": "tool",
                "entity_id": tool_name,
                "state": tool_cb.state.value,
                "message": f"Tool '{tool_name}' circuit breaker is open (too many errors)",
            }

    return None


def record_outcome(
    agent_name: str,
    tool_name: str | None,
    success: bool,
) -> None:
    """Record success/error for circuit breaker tracking."""
    agent_cb = get_breaker(agent_name, "agent")
    if success:
        agent_cb.record_success()
    else:
        agent_cb.record_error()

    if tool_name:
        tool_cb = get_breaker(tool_name, "tool")
        if success:
            tool_cb.record_success()
        else:
            tool_cb.record_error()


def list_breakers() -> list[dict[str, Any]]:
    """List all circuit breakers and their current state."""
    return [cb.to_dict() for cb in _breakers.values()]


def reset_breaker(entity_id: str, entity_type: str = "agent") -> bool:
    """Manually reset a circuit breaker to CLOSED."""
    key = f"{entity_type}:{entity_id}"
    if key in _breakers:
        _breakers[key]._close()
        logger.info("Circuit breaker manually reset: %s %s", entity_type, entity_id)
        return True
    return False

"""Client-side halt enforcement.

A halt is an operator-imposed kill-switch: the operator creates one via
``POST /v1/halts`` on the receiver, and any SDK whose Client polls the
sync endpoint sees it within one poll cycle and starts raising
``StrathonHaltExceeded`` at every tool-call boundary.

This mirrors ``PolicyEnforcer`` deliberately. The two are kept separate
classes (rather than one combined enforcer) because:

* The contracts differ. Policies are CEL-evaluated against per-call
  span context; halts are looked up by scope (agent_id or project) and
  apply unconditionally to anything in scope.
* The exception types differ. Block raises ``StrathonPolicyBlocked``,
  halt raises ``StrathonHaltExceeded`` — operators handling the two
  cases need the distinction.
* The lifecycles differ. Policies are durable rules; halts are
  transient kill-switches that operators clear when the incident's
  over. A combined enforcer would mix two different change cadences in
  one cache.

The extra cost is one daemon thread per Client. Negligible.

Failure model: fail-OPEN
========================

If the receiver is unreachable at SDK startup, the halt cache stays
empty and every call allows. If the receiver becomes unreachable
mid-session, the last-known cache stays in force until the next
successful refresh. Either way, an outage of the receiver does NOT
halt every agent in production — which would be the worst possible
failure mode and the reason LaunchDarkly fails their flag SDKs open
too.

Operators who want fail-closed semantics will get that knob in a
future commit alongside the budget-rollup work. The default is
fail-open.

Scope matching
==============

The receiver's sync payload carries one entry per active halt:

    {"id": 1, "scope": "agent", "scope_value": "agent-7",
     "state": "halted", "reason": "killswitch"}

The SDK matches per-call by:

* Project-scope halts (``scope_value is None``): match every call.
* Agent-scope halts: match when the call's
  ``attrs["strathon.agent.id"]`` (or ``gen_ai.agent.id``) equals
  ``scope_value``.

A call matched by ANY active halt is halted. If multiple halts match,
the most recently set wins (rows arrive sorted by ``set_at DESC`` from
the receiver) — but in practice that distinction doesn't matter for
the raised exception; the caller just needs to know "stopped."
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from strathon.policy.types import ALLOW_HALT, HaltDecision

logger = logging.getLogger(__name__)


class HaltEnforcer:
    """In-process halt cache + evaluator.

    Thread-safe: a single instance is shared by all framework
    integrations on a Client. Refreshed in the background.
    """

    def __init__(
        self,
        endpoint: str,
        api_key: Optional[str] = None,
        project_id: Optional[str] = None,
        refresh_interval_sec: float = 1.0,
        request_timeout_sec: float = 5.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._project_id = project_id
        self._refresh_interval_sec = refresh_interval_sec
        self._request_timeout_sec = request_timeout_sec

        self._lock = threading.RLock()
        self._halts: List[Dict[str, Any]] = []
        self._last_refresh_at: float = 0.0
        self._last_refresh_error: Optional[str] = None

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- Public API ----

    def start(self) -> None:
        """Kick off initial fetch and background refresh thread.

        The synchronous refresh in start() lets the first check_halt
        call after construction have data, rather than uniformly
        returning allow until the first background tick.
        """
        self.refresh()
        if self._thread is not None:
            return
        t = threading.Thread(
            target=self._refresh_loop,
            name="strathon-halt-refresh",
            daemon=True,
        )
        self._thread = t
        t.start()

    def stop(self) -> None:
        """Signal the background thread to exit."""
        self._stop_event.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        self._thread = None

    def refresh(self) -> bool:
        """Fetch the latest halt set from the receiver.

        Returns True on success, False on failure. Failures don't
        clear the cache; the previous halts remain in force until a
        successful refresh updates them. This is the fail-open
        property: an outage doesn't suddenly halt every agent.
        """
        url = f"{self._endpoint}/v1/intervention/sync"
        body = json.dumps({}).encode("utf-8")
        try:
            req = Request(
                url, data=body, method="POST",
                headers={
                    **self._auth_headers(),
                    "Content-Type": "application/json",
                },
            )
            with urlopen(req, timeout=self._request_timeout_sec) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except URLError as exc:
            with self._lock:
                self._last_refresh_error = f"network error: {exc}"
            logger.debug("HaltEnforcer refresh failed: %s", exc)
            return False
        except Exception as exc:
            with self._lock:
                self._last_refresh_error = f"unexpected error: {exc}"
            logger.exception("HaltEnforcer refresh failed unexpectedly")
            return False

        halts = payload.get("halts") or []
        if not isinstance(halts, list):
            logger.warning(
                "HaltEnforcer: server returned non-list halts: %r",
                type(halts).__name__,
            )
            halts = []

        with self._lock:
            self._halts = halts
            self._last_refresh_at = time.time()
            self._last_refresh_error = None
        logger.debug("HaltEnforcer: loaded %d active halts", len(halts))
        return True

    def check_halt(self, span_context: Dict[str, Any]) -> HaltDecision:
        """Return the first matching halt, or ALLOW if none matches.

        Match rules:
          * project-scope halts (scope_value is None) match every call
          * agent-scope halts match when span_context's
            ``strathon.agent.id`` or ``gen_ai.agent.id`` equals
            scope_value

        Halts are evaluated in the order returned by the server (most
        recently set first). The first match wins; this means the
        most recent active halt is what the caller sees in the raised
        exception, which is what an operator who just clicked the
        kill-switch button expects.
        """
        with self._lock:
            halts = list(self._halts)

        if not halts:
            return ALLOW_HALT

        attrs = span_context.get("attrs") or {}
        agent_id = (
            attrs.get("strathon.agent.id")
            or attrs.get("gen_ai.agent.id")
        )

        for h in halts:
            scope = h.get("scope")
            scope_value = h.get("scope_value")
            if scope == "project":
                return HaltDecision(
                    action="halt",
                    halt_id=h.get("id"),
                    scope="project",
                    scope_value=None,
                    reason=h.get("reason"),
                    state=h.get("state"),
                )
            if scope == "agent" and scope_value and agent_id == scope_value:
                return HaltDecision(
                    action="halt",
                    halt_id=h.get("id"),
                    scope="agent",
                    scope_value=scope_value,
                    reason=h.get("reason"),
                    state=h.get("state"),
                )

        return ALLOW_HALT

    # ---- Introspection (mostly for tests + debugging) ----

    @property
    def halts(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._halts)

    @property
    def last_refresh_error(self) -> Optional[str]:
        with self._lock:
            return self._last_refresh_error

    def set_halts_for_testing(self, halts: List[Dict[str, Any]]) -> None:
        """Bypass the network for tests."""
        with self._lock:
            self._halts = list(halts)
            self._last_refresh_at = time.time()

    # ---- Internals ----

    def _auth_headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _refresh_loop(self) -> None:
        while not self._stop_event.is_set():
            # Sleep first; we already did a synchronous refresh in start()
            if self._stop_event.wait(timeout=self._refresh_interval_sec):
                return
            try:
                self.refresh()
            except Exception:
                logger.exception("HaltEnforcer refresh loop error")


__all__ = ["HaltEnforcer"]

"""Client-side policy enforcement for runtime intervention.

Lives in the SDK and is consulted by framework integrations before they
allow a tool call / LLM call to proceed. It maintains a cached set of
policies pulled from the receiver and refreshed periodically in the
background.

Decision flow:
    1. Framework integration constructs the candidate span_context dict
       (the same shape the receiver would see).
    2. Calls client.check_policy(span_context).
    3. Returns a PolicyDecision:
         - 'allow'  -> proceed normally
         - 'block'  -> raise StrathonPolicyBlocked
         - 'steer'  -> return the replacement string in place of real output

Server-only actions ('log', 'alert') are ignored here; the receiver
handles them when the span is later ingested.

Policies are evaluated in descending priority order. The first matching
policy whose action affects control flow ('block' or 'steer') wins.
'log' / 'alert' matches do not short-circuit and are skipped client-side.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from strathon.policy.expression import evaluate
from strathon.policy.types import ALLOW, Policy, PolicyDecision

logger = logging.getLogger(__name__)


class PolicyEnforcer:
    """In-process policy cache + evaluator for the SDK.

    Thread-safe: a single PolicyEnforcer is shared by all framework
    integrations on a Client. Policies are refreshed in the background.
    """

    def __init__(
        self,
        endpoint: str,
        api_key: Optional[str] = None,
        project_id: Optional[str] = None,
        refresh_interval_sec: float = 30.0,
        request_timeout_sec: float = 5.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._project_id = project_id
        self._refresh_interval_sec = refresh_interval_sec
        self._request_timeout_sec = request_timeout_sec

        self._lock = threading.RLock()
        self._policies: List[Policy] = []
        self._last_refresh_at: float = 0.0
        self._last_refresh_error: Optional[str] = None

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- Public API ----

    def start(self) -> None:
        """Kick off initial fetch and background refresh thread."""
        # One synchronous attempt so the first check_policy call has data
        self.refresh()
        if self._thread is not None:
            return
        t = threading.Thread(
            target=self._refresh_loop,
            name="strathon-policy-refresh",
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
        """Fetch the latest policy set from the receiver. Returns True on success."""
        url = f"{self._endpoint}/v1/policies"
        if self._project_id:
            url = f"{url}?project_id={self._project_id}"
        try:
            req = Request(url, headers=self._auth_headers())
            with urlopen(req, timeout=self._request_timeout_sec) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except URLError as exc:
            with self._lock:
                self._last_refresh_error = f"network error: {exc}"
            logger.debug("PolicyEnforcer refresh failed: %s", exc)
            return False
        except Exception as exc:
            with self._lock:
                self._last_refresh_error = f"unexpected error: {exc}"
            logger.exception("PolicyEnforcer refresh failed unexpectedly")
            return False

        try:
            policies = [Policy.from_dict(p) for p in payload.get("policies", [])]
        except Exception:
            logger.exception("PolicyEnforcer: malformed policy payload from server")
            return False

        # Sort by priority desc so first match wins
        policies.sort(key=lambda p: (-p.priority, p.name))

        with self._lock:
            self._policies = policies
            self._last_refresh_at = time.time()
            self._last_refresh_error = None
        logger.debug("PolicyEnforcer: loaded %d policies", len(policies))
        return True

    def check_policy(self, span_context: Dict[str, Any]) -> PolicyDecision:
        """Evaluate active policies against a candidate action.

        Returns the highest-priority decision that affects control flow:
            - PolicyDecision(action='block', ...) if any block rule matches
            - PolicyDecision(action='steer', ...) if any steer rule matches
            - ALLOW otherwise

        'log' and 'alert' actions are server-side and do not affect the
        return value (they are applied later when the span is ingested).
        """
        with self._lock:
            policies = list(self._policies)

        if not policies:
            return ALLOW

        for policy in policies:
            if not policy.enabled:
                continue
            if policy.action not in {"block", "steer"}:
                continue
            if not _span_matches_applies_to(span_context, policy.applies_to):
                continue
            if not evaluate(policy.match_expression, span_context):
                continue

            if policy.action == "block":
                message = (
                    policy.action_config.get("message")
                    or f"Blocked by Strathon policy '{policy.name}'"
                )
                return PolicyDecision(
                    action="block",
                    policy_id=policy.id,
                    policy_name=policy.name,
                    message=message,
                )
            # steer
            replacement = (
                policy.action_config.get("replacement")
                or f"[Strathon policy '{policy.name}' redirected this call]"
            )
            return PolicyDecision(
                action="steer",
                policy_id=policy.id,
                policy_name=policy.name,
                replacement=replacement,
            )

        return ALLOW

    # ---- Inspection / testing helpers ----

    @property
    def policies(self) -> List[Policy]:
        """Snapshot of currently active policies, in eval order."""
        with self._lock:
            return list(self._policies)

    @property
    def last_refresh_error(self) -> Optional[str]:
        with self._lock:
            return self._last_refresh_error

    def set_policies_for_testing(self, policies: List[Policy]) -> None:
        """Manually seed policies, bypassing the receiver. Tests only."""
        sorted_policies = sorted(policies, key=lambda p: (-p.priority, p.name))
        with self._lock:
            self._policies = sorted_policies
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
                logger.exception("PolicyEnforcer refresh loop error")


def _span_matches_applies_to(
    span_context: Dict[str, Any], applies_to: List[str]
) -> bool:
    """Check whether a span's name matches the policy's applies_to filter.

    applies_to is a list of substrings; empty list means "applies to all".
    Example: ["tool", "llm"] matches any span whose name contains "tool" or "llm".
    """
    if not applies_to:
        return True
    name = span_context.get("name") or ""
    return any(token in name for token in applies_to)

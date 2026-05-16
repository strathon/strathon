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

from strathon.exceptions import StrathonReceiverUnreachable
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
        fail_closed: bool = False,
        fail_closed_max_staleness_sec: float = 60.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._project_id = project_id
        self._refresh_interval_sec = refresh_interval_sec
        self._request_timeout_sec = request_timeout_sec
        self._fail_closed = fail_closed
        self._fail_closed_max_staleness_sec = fail_closed_max_staleness_sec

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

        Fail-closed: when ``fail_closed=True`` was set on this
        enforcer, this method raises ``StrathonReceiverUnreachable``
        instead of returning a decision whenever the cached policy
        state is older than ``fail_closed_max_staleness_sec``. The
        default ``fail_closed=False`` preserves the historical
        fail-open behavior — stale state continues to be used.
        """
        self._assert_fresh_if_fail_closed()

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

    def set_last_refresh_for_testing(self, ts: float) -> None:
        """Force the last-refresh timestamp for fail-closed staleness tests."""
        with self._lock:
            self._last_refresh_at = ts

    # ---- Internals ----

    def _assert_fresh_if_fail_closed(self) -> None:
        """Raise :class:`StrathonReceiverUnreachable` when fail-closed is
        on AND the cached state is older than the configured threshold.

        No-op when ``fail_closed`` is False (the default). The check is
        a single read-and-compare on the per-instance lock, so it adds
        microseconds to the tool-boundary path even at high QPS.
        """
        if not self._fail_closed:
            return
        with self._lock:
            last = self._last_refresh_at
        # When no refresh has ever succeeded, ``last`` is 0.0 — staleness
        # is effectively infinite and the threshold check naturally fails.
        staleness = time.time() - last
        if staleness > self._fail_closed_max_staleness_sec:
            raise StrathonReceiverUnreachable(
                (
                    f"policy cache stale by {staleness:.1f}s "
                    f"(max {self._fail_closed_max_staleness_sec:.1f}s); "
                    "receiver may be unreachable. fail_closed=True is on, "
                    "so this tool call is refused."
                ),
                subsystem="policy_enforcer",
                staleness_seconds=staleness,
                max_staleness_seconds=self._fail_closed_max_staleness_sec,
            )

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

    Empty applies_to means "match every span". Otherwise each token in
    the list is treated as a dot-segment-path: the token matches the
    name if and only if the token aligns with one or more whole
    dot-separated segments of the name. The OR of all tokens is the
    overall match.

    Examples (name -> tokens that match):
        "langgraph.tool.send_email"  -> "tool", "langgraph",
                                        "send_email", "langgraph.tool",
                                        "tool.send_email",
                                        "langgraph.tool.send_email"
        "langgraph.tool.send_email"  -/->  "send", "ool", "tool.send"

    The rule rejects raw substring matches like "tool" against "pool"
    or against the middle of an unrelated segment. This matches the
    pattern operators already know from cloud IAM resource paths,
    Kubernetes label selectors, and DNS suffix matching.
    """
    if not applies_to:
        return True
    name = span_context.get("name") or ""
    if not name:
        return False
    return any(_segment_path_match(name, token) for token in applies_to)


def _segment_path_match(name: str, token: str) -> bool:
    """True iff ``token`` aligns with whole dot-separated segments of ``name``.

    Implemented as four cases that together cover every legal alignment
    of a token within a dot-segmented path. The dot-padding trick on the
    interior case turns "segment X exists at any position" into a single
    substring search.
    """
    if not token:
        return False
    if name == token:
        return True
    return (
        name.startswith(token + ".")
        or name.endswith("." + token)
        or ("." + token + ".") in name
    )

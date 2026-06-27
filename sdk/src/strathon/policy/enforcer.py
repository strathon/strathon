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
from strathon.policy.throttle import ThrottleStore
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
        environment: Optional[str] = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._project_id = project_id
        self._environment = environment
        self._refresh_interval_sec = refresh_interval_sec
        self._request_timeout_sec = request_timeout_sec
        self._fail_closed = fail_closed
        self._fail_closed_max_staleness_sec = fail_closed_max_staleness_sec

        self._lock = threading.RLock()
        self._policies: List[Policy] = []
        self._last_refresh_at: float = 0.0
        self._last_refresh_error: Optional[str] = None
        # Project-level "what happens to unmatched calls" knob. The
        # receiver returns this alongside the policy list in /v1/policies.
        # 'allow' (the historical default) admits unmatched calls;
        # 'block' flips the project into allow-list mode where a call
        # must be explicitly admitted by an action="allow" policy or it
        # is denied at the tool boundary. Defaults to 'allow' on first
        # construction so the SDK behaves identically to pre-allow-list
        # behavior until a refresh tells us otherwise.
        self._intervention_default_action: str = "allow"

        # Per-policy token buckets for the throttle action. State is
        # process-local, like the policy cache itself; multi-replica
        # SDKs each see their own buckets. See docs/intervention.md
        # for the trade-off note (matches receiver/rate_limit.py).
        self._throttle_store = ThrottleStore()

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

        # Read the project-level intervention default action. Older
        # receivers (pre-allow-list) won't send this field; we fall
        # back to "allow" to preserve their behavior.
        default_action_raw = payload.get("intervention_default_action", "allow")
        if default_action_raw not in {"allow", "block"}:
            logger.warning(
                "PolicyEnforcer: server returned unknown "
                "intervention_default_action %r; defaulting to 'allow'",
                default_action_raw,
            )
            default_action = "allow"
        else:
            default_action = default_action_raw

        with self._lock:
            self._policies = policies
            self._intervention_default_action = default_action
            self._last_refresh_at = time.time()
            self._last_refresh_error = None
        logger.debug(
            "PolicyEnforcer: loaded %d policies, default_action=%s",
            len(policies), default_action,
        )
        return True

    def check_policy(self, span_context: Dict[str, Any]) -> PolicyDecision:
        """Evaluate active policies against a candidate action.

        Iteration is priority-descending. The first matching policy
        whose action affects control flow ('block', 'steer',
        'throttle' [when denied], or 'allow') short-circuits and that
        decision is returned. ``log`` and ``alert`` actions are
        server-side only and don't affect the return value.

        At the end of the iteration (no short-circuit fired) the
        project's ``intervention_default_action`` decides:

          * ``"allow"`` (default) — returns ALLOW. Pre-allow-list
            behavior; unmatched calls go through.
          * ``"block"`` — allow-list mode. Returns a synthetic block
            decision with ``policy_id=None`` and a "no policy
            explicitly allowed" message. The caller raises
            ``StrathonPolicyBlocked`` from this just like any other
            block decision.

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
            default_action = self._intervention_default_action

        if not policies:
            return self._default_decision(default_action)

        # Make the deployment environment queryable in CEL. The Client knows
        # its environment (it ships it as the deployment.environment span
        # resource attribute), but resource attributes do not flow into the
        # per-call attrs the policy evaluator sees. Merge it in here, once,
        # under strathon.project.environment so a single policy like
        #   attrs["strathon.project.environment"] == "production"
        # works uniformly across every enforcement surface. Only set it when
        # the caller has not already provided one, so an explicit per-call
        # value still wins.
        if self._environment is not None:
            attrs = span_context.get("attrs")
            if isinstance(attrs, dict) and "strathon.project.environment" not in attrs:
                attrs["strathon.project.environment"] = self._environment

        for policy in policies:
            if not policy.enabled:
                continue
            if policy.shadow:
                # Shadow policies record server-side; they never enforce.
                # Enforcing one here would block live traffic during what the
                # operator believes is a dry run.
                continue
            if policy.action not in {"block", "steer", "throttle", "allow", "require_approval"}:
                continue
            if not _span_matches_applies_to(span_context, policy.applies_to):
                continue
            if not evaluate(policy.match_expression, span_context):
                continue

            if policy.action == "block":
                message = (
                    (policy.action_config or {}).get("message")
                    or f"Blocked by Strathon policy '{policy.name}'"
                )
                return PolicyDecision(
                    action="block",
                    policy_id=policy.id,
                    policy_name=policy.name,
                    message=message,
                )
            if policy.action == "allow":
                # Explicit allow: short-circuits subsequent policies.
                # In allow-list mode this is how a call gets admitted;
                # outside allow-list mode it lets an operator carve out
                # a specific tool from being affected by lower-priority
                # block/steer rules. Priority ordering still applies, so
                # a higher-priority block beats a lower-priority allow.
                return PolicyDecision(
                    action="allow",
                    policy_id=policy.id,
                    policy_name=policy.name,
                )
            if policy.action == "throttle":
                decision = self._evaluate_throttle(policy, span_context)
                if decision is None:
                    # Bucket admitted this call; throttle is a no-op
                    # for this specific invocation and we fall through
                    # to keep evaluating lower-priority rules. A more
                    # restrictive block/steer further down still wins.
                    continue
                return decision
            if policy.action == "require_approval":
                # Defensively coerce timeout_seconds: a malformed value (None,
                # a non-numeric string, a bool) must NOT raise out of
                # check_policy, because every adapter's policy-check handler
                # fails OPEN on exception — so a bad config value would silently
                # disable approval. Fall back to 300s on anything invalid, the
                # same posture _evaluate_throttle takes for window_seconds.
                raw_timeout = (policy.action_config or {}).get("timeout_seconds", 300)
                if isinstance(raw_timeout, bool) or not isinstance(
                    raw_timeout, (int, float)
                ):
                    timeout = 300
                else:
                    timeout = int(raw_timeout)
                    if timeout <= 0:
                        timeout = 300
                # approvers_required (the N in N-of-M). Same fail-safe posture:
                # an invalid value must never raise out of check_policy, so it
                # falls back to 1 (single sign-off) rather than disabling the
                # policy. The receiver enforces the threshold and dedupes
                # distinct approvers.
                raw_approvers = (policy.action_config or {}).get(
                    "approvers_required", 1
                )
                if isinstance(raw_approvers, bool) or not isinstance(
                    raw_approvers, int
                ):
                    approvers_required = 1
                else:
                    approvers_required = raw_approvers if raw_approvers >= 1 else 1
                return PolicyDecision(
                    action="require_approval",
                    policy_id=policy.id,
                    policy_name=policy.name,
                    message=(
                        (policy.action_config or {}).get("message")
                        or f"Tool call requires approval per policy '{policy.name}'"
                    ),
                    timeout_seconds=timeout,
                    approvers_required=approvers_required,
                )
            # steer
            replacement = (
                (policy.action_config or {}).get("replacement")
                or f"[Strathon policy '{policy.name}' redirected this call]"
            )
            return PolicyDecision(
                action="steer",
                policy_id=policy.id,
                policy_name=policy.name,
                replacement=replacement,
            )

        return self._default_decision(default_action)

    def _default_decision(self, default_action: str) -> PolicyDecision:
        """Build the end-of-iteration decision for ``default_action``.

        ``"allow"`` returns the shared ALLOW singleton (no allocation
        on the hot path). ``"block"`` returns a synthetic block
        decision with no associated policy — callers see
        ``decision.policy_id is None`` and a message that names
        allow-list mode explicitly, which makes the cause obvious
        when the exception lands in an operator's logs.
        """
        if default_action == "block":
            return PolicyDecision(
                action="block",
                policy_id=None,
                policy_name=None,
                message=(
                    "no policy explicitly allowed this call "
                    "(project is in allow-list mode: "
                    "intervention_default_action=block)"
                ),
            )
        return ALLOW

    def _evaluate_throttle(
        self, policy: Policy, span_context: Dict[str, Any],
    ) -> Optional[PolicyDecision]:
        """Consult the token bucket for ``policy`` and return a throttle
        decision when the bucket is empty, or ``None`` when this call
        was admitted.

        Server-side validation ensures the config has the right shape
        before the policy lands here, but the SDK still defends against
        a malformed cache (e.g. an older SDK reading a newer policy
        format). On malformed config we log once and admit the call —
        the alternative is silently throttling agents based on a
        misconfigured rule, which is worse than letting it through.
        """
        cfg = policy.action_config or {}
        max_calls = cfg.get("max_calls")
        window_seconds = cfg.get("window_seconds")
        if (
            not isinstance(max_calls, int)
            or isinstance(max_calls, bool)
            or max_calls <= 0
            or not isinstance(window_seconds, (int, float))
            or isinstance(window_seconds, bool)
            or window_seconds <= 0
        ):
            logger.warning(
                "PolicyEnforcer: throttle policy %r has malformed action_config "
                "(max_calls=%r, window_seconds=%r); admitting call",
                policy.name, max_calls, window_seconds,
            )
            return None

        scope = cfg.get("scope", "agent")
        if scope == "global":
            scope_key = "global"
        else:
            # Default "agent". A missing agent id falls back to the
            # span name so a misconfigured agent doesn't bypass the
            # bucket entirely by simply not setting the attribute.
            attrs = span_context.get("attrs") or {}
            agent_id = (
                attrs.get("strathon.agent.id")
                or attrs.get("gen_ai.agent.id")
            )
            scope_key = str(agent_id) if agent_id else f"unknown:{span_context.get('name', '')}"

        allowed, retry_after = self._throttle_store.consume(
            policy_id=policy.id,
            scope_key=scope_key,
            max_calls=max_calls,
            window_seconds=float(window_seconds),
        )
        if allowed:
            return None

        message = (
            cfg.get("message")
            or f"Rate limit exceeded for Strathon policy '{policy.name}'"
        )
        return PolicyDecision(
            action="throttle",
            policy_id=policy.id,
            policy_name=policy.name,
            message=message,
            retry_after_seconds=retry_after,
        )

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

    @property
    def intervention_default_action(self) -> str:
        """The project's current default for unmatched calls.

        Mirrors what the SDK last received from the receiver's
        ``/v1/policies`` response. Useful for tests and operator
        tooling that wants to confirm the SDK has the expected
        allow-list-mode state.
        """
        with self._lock:
            return self._intervention_default_action

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

    def set_intervention_default_action_for_testing(self, value: str) -> None:
        """Force the project's default action without going through the
        receiver. Tests only — production state arrives via refresh().
        """
        if value not in {"allow", "block"}:
            raise ValueError(
                f"intervention_default_action must be 'allow' or 'block', "
                f"got {value!r}"
            )
        with self._lock:
            self._intervention_default_action = value

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

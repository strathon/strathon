"""Strathon egress proxy — mitmproxy addon for agent HTTP traffic.

Intercepts all outbound HTTP from agent processes. Evaluates against
Strathon policies via the receiver API. Scans request and response
bodies for credential leakage using the built-in 70+ pattern library.

Usage:
    pip install strathon[proxy]    # or: pip install mitmproxy
    mitmdump -s receiver/egress_proxy.py \\
        --set strathon_url=http://localhost:4318 \\
        --set strathon_key=stra_...

    # Agent process:
    export HTTP_PROXY=http://localhost:8080
    export HTTPS_PROXY=http://localhost:8080
    python my_agent.py

The proxy blocks requests that match Strathon policies and redacts
credentials found in responses. Blocked requests return 403 with
an X-Strathon-Block-Reason header.

Research: Anthropic MCP specification, mitmproxy addon API,
OWASP egress filtering best practices.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("strathon.egress_proxy")

try:
    from mitmproxy import http, ctx as mitmctx

    class StrathonEgressAddon:
        """mitmproxy addon that enforces Strathon policies on egress traffic."""

        def __init__(self):
            self.strathon_url = os.environ.get(
                "STRATHON_EGRESS_RECEIVER_URL", "http://localhost:4318"
            )
            self.api_key = os.environ.get("STRATHON_API_KEY", "")
            self._credential_patterns = None
            # Pulled policy list (same source the SDK uses: fetch from
            # /v1/policies and evaluate CEL locally). Pulled once at startup and
            # again on any mitmproxy option change; static for the process
            # lifetime otherwise (no periodic refresh).
            self._policies: list[dict[str, Any]] = []
            self._policies_loaded = False
            # Project-level allow-list toggle from /v1/policies; "allow"
            # (admit unmatched) or "block" (default-deny). Set on refresh.
            self._default_action = "allow"

        def load(self, loader):
            loader.add_option(
                "strathon_url", str, self.strathon_url,
                "Strathon receiver URL",
            )
            loader.add_option(
                "strathon_key", str, self.api_key,
                "Strathon API key",
            )

        def configure(self, updates):
            if "strathon_url" in updates:
                self.strathon_url = mitmctx.options.strathon_url
            if "strathon_key" in updates:
                self.api_key = mitmctx.options.strathon_key
            # Refresh the policy list whenever config changes (and on first run).
            self._refresh_policies()
            # Log where we pull policy from and how many rules loaded, so an
            # operator can confirm the addon is live and configured. An egress
            # proxy that is loaded but pulling zero policies (e.g. wrong URL or
            # key) should be visible, not silent.
            if self.api_key and self.strathon_url:
                logger.info(
                    "egress: active, pulling policies from %s (%d loaded)",
                    self.strathon_url,
                    len(self._policies or []),
                )
            else:
                logger.warning(
                    "egress: addon loaded but strathon_url/strathon_key not set "
                    "— no policies will be enforced until both are provided"
                )

        def _refresh_policies(self) -> None:
            """Pull the project's policies from /v1/policies (the real
            endpoint the SDK uses). Evaluation happens locally; there is no
            per-request round-trip and therefore no per-request fail-open.
            """
            if not (self.api_key and self.strathon_url):
                return
            try:
                import httpx
                with httpx.Client(timeout=5.0) as client:
                    resp = client.get(
                        f"{self.strathon_url}/v1/policies",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        params={"enabled": "true"},
                    )
                if resp.status_code == 200:
                    payload = resp.json()
                    self._policies = payload.get("policies", [])
                    # Honor allow-list (default-deny) mode: a project whose
                    # intervention_default_action is "block" denies any request
                    # that matches no policy. Same posture as the SDK enforcer
                    # so a project's security stance is identical across every
                    # surface, not allow-by-default only at the egress boundary.
                    self._default_action = payload.get(
                        "intervention_default_action", "allow"
                    )
                    self._policies_loaded = True
            except Exception:
                logger.exception("egress: failed to refresh policies")

        def _get_patterns(self):
            """Lazy-load credential patterns."""
            if self._credential_patterns is None:
                try:
                    from credential_patterns import PATTERNS
                    self._credential_patterns = PATTERNS
                except ImportError:
                    self._credential_patterns = []
            return self._credential_patterns

        def request(self, flow: http.HTTPFlow) -> None:
            """Intercept outbound request. Block if policy denies it."""
            url = flow.request.pretty_url
            method = flow.request.method
            body = flow.request.get_text() or ""

            # Scan request body for credentials.
            credentials_found = self._scan_for_credentials(body)
            if credentials_found:
                flow.response = http.Response.make(
                    403,
                    json.dumps({
                        "error": "Blocked by Strathon: credentials detected in request body",
                        "patterns": [c["pattern_name"] for c in credentials_found],
                    }).encode(),
                    {"Content-Type": "application/json",
                     "X-Strathon-Block-Reason": "credential-leak"},
                )
                logger.warning(
                    "Blocked request to %s: %d credential(s) found",
                    url, len(credentials_found),
                )
                return

            # Evaluate against pulled policies LOCALLY (no per-request HTTP).
            verdict = self._evaluate_policies(method, url)
            action = verdict.get("action", "allow")
            # block: hard 403. throttle / require_approval / steer cannot be
            # meaningfully serviced on a synchronous in-flight network request
            # (no rate-bucket wait, no interactive approval, no result to
            # substitute for a raw egress connection), so they fall CLOSED to
            # a 403 rather than silently forwarding. Only "allow" (and "log",
            # which is allow-with-record) proceeds. This mirrors the fail-
            # closed posture on the policy-eval error path below.
            if action not in ("allow", "log"):
                reason_map = {
                    "block": "policy",
                    "throttle": "policy-throttle",
                    "require_approval": "policy-approval-required",
                    "steer": "policy-steer-unenforceable",
                }
                block_reason = reason_map.get(action, "policy")
                flow.response = http.Response.make(
                    403,
                    json.dumps({
                        "error": f"Blocked by policy: {verdict.get('policy_name', 'unknown')}",
                        "action": action,
                    }).encode(),
                    {"Content-Type": "application/json",
                     "X-Strathon-Block-Reason": block_reason},
                )
                logger.warning(
                    "Egress request to %s denied (action=%s) by policy %s",
                    url, action, verdict.get("policy_name", ""),
                )
                return

        def _evaluate_policies(self, method: str, url: str) -> dict[str, Any]:
            """Evaluate the pulled policies against this request, locally.

            Maps the HTTP request to the same span-context shape the rest of
            Strathon uses (tool name = http.<method>, url in attrs) and runs
            the shared CEL evaluator. Returns the highest-priority matching
            action, or allow if none match.
            """
            if not self._policies:
                # No policies loaded. Honor the project's allow-list posture
                # exactly as the no-match branch below does: in default-deny
                # (allow-list) mode an ungoverned request falls closed, matching
                # the SDK enforcer and MCP gateway. Returning allow here
                # unconditionally was a silent-allow gap — the egress surface
                # admitting traffic that the other two surfaces deny for the
                # same project state.
                if getattr(self, "_default_action", "allow") == "block":
                    return {"action": "block", "policy_name": "_default_deny"}
                return {"action": "allow"}
            try:
                from policies import evaluate_for_span
                tool_name = f"http.{method.lower()}"
                attrs = {
                    "strathon.tool.name": tool_name,
                    "gen_ai.tool.name": tool_name,
                    "strathon.http.url": url,
                    "strathon.source": "egress_proxy",
                }
                matches = evaluate_for_span(self._policies, tool_name, attrs)
                # Shadow policies never enforce; drop them before picking the
                # top action (same filter as the SDK enforcer and MCP gateway).
                matches = [m for m in matches if not m.get("shadow", False)]
                if not matches:
                    # Allow-list mode: no policy matched and the project is
                    # default-deny, so fall closed.
                    if getattr(self, "_default_action", "allow") == "block":
                        return {
                            "action": "block",
                            "policy_name": "_default_deny",
                        }
                    return {"action": "allow"}
                top = matches[0]
                return {"action": top.get("action", "allow"),
                        "policy_name": top.get("name", "")}
            except Exception:
                logger.exception("egress: local policy evaluation failed")
                # Fail-closed on the policy path: if evaluation errors, block.
                return {"action": "block", "policy_name": "_fail_closed"}

        def response(self, flow: http.HTTPFlow) -> None:
            """Scan response body for credential leakage."""
            if flow.response and flow.response.content:
                body = flow.response.get_text() or ""
                credentials_found = self._scan_for_credentials(body)
                if credentials_found:
                    from credential_patterns import redact_credentials
                    redacted, count = redact_credentials(body)
                    flow.response.set_text(redacted)
                    flow.response.headers["X-Strathon-Redacted"] = str(count)
                    logger.warning(
                        "Redacted %d credential(s) from response %s",
                        count, flow.request.pretty_url,
                    )

        def _scan_for_credentials(self, text: str) -> list[dict[str, Any]]:
            """Scan text for credential patterns."""
            if not text or len(text) < 10:
                return []
            findings = []
            for p in self._get_patterns():
                if p.pattern.search(text):
                    findings.append({
                        "pattern_id": p.id,
                        "pattern_name": p.name,
                        "severity": p.severity,
                    })
            return findings

    addons = [StrathonEgressAddon()]

except ImportError:
    # mitmproxy not installed. This file can still be imported for
    # documentation/testing without mitmproxy dependency.
    pass

"""MCP Security Gateway.

Sits between an AI agent and an MCP (Model Context Protocol) server.
Intercepts MCP JSON-RPC requests, evaluates tools/call against the
project's enabled Strathon policies, and either forwards to the upstream
MCP server or blocks.

    Agent -> Strathon MCP Gateway -> upstream MCP server
             (policy evaluation, in-process)

Design notes:
  * Evaluation is IN-PROCESS. The gateway runs inside the receiver and
    calls the same policy primitive the OTLP ingest path uses
    (policies.evaluate_for_span), so a tool call is judged by the exact
    same logic and the exact same enabled policies as everything else.
    There is no self-HTTP round-trip.
  * FAIL-CLOSED by default. If policy evaluation cannot complete, a
    tools/call is BLOCKED, not allowed. A security gateway that allows
    traffic when its policy engine is unavailable is a bypass-by-DoS.
    Operators who prefer availability over strictness can set
    fail_open=True explicitly.
  * The mapping to a policy span context mirrors the SDK's tool spans:
    span name = the MCP tool name, attrs carry the tool name and the
    JSON-encoded arguments, so existing tool-name / argument policies
    match MCP calls identically to framework-instrumented calls.

Supported MCP methods:
  tools/call     -> evaluated (block / require_approval / allow). Main
                    enforcement point. throttle/steer are treated as
                    allow-with-log at the gateway (the gateway cannot
                    substitute a tool result the way an in-process SDK
                    adapter can; that enforcement stays in the SDK).
  tools/list     -> forwarded; blocked tools filtered from the result.
  resources/read -> forwarded; response scanned for leaked credentials.
  everything else -> forwarded unchanged.

Research: Anthropic MCP specification (modelcontextprotocol.io),
JSON-RPC 2.0, OWASP Agentic ASI04 (agent-to-agent) / ASI08 (access
control). Reviewed before implementation.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("strathon.mcp_gateway")

# JSON-RPC error codes used by the gateway.
_ERR_BLOCKED = -32040          # policy block (custom application range)
_ERR_APPROVAL_REQUIRED = -32041
_ERR_POLICY_UNAVAILABLE = -32042
_ERR_UPSTREAM = -32603         # JSON-RPC internal error


class MCPSecurityGateway:
    """Evaluates MCP tool calls against a project's Strathon policies.

    The gateway is constructed per request-context with the already-loaded
    set of enabled policies (the caller, a receiver route, loads them with
    repositories.policies.list_policies(only_enabled=True) — the same call
    ingest uses). Keeping policy loading in the route keeps this class a
    pure, easily-tested evaluator with no DB dependency.
    """

    def __init__(
        self,
        upstream_url: str,
        policies: list[dict[str, Any]],
        *,
        blocked_tools: Optional[list[str]] = None,
        scan_responses: bool = True,
        fail_open: bool = False,
        http_timeout: float = 30.0,
    ):
        self.upstream_url = upstream_url
        self.policies = policies or []
        self.blocked_tools = set(blocked_tools or [])
        self.scan_responses = scan_responses
        self.fail_open = fail_open
        self.http_timeout = http_timeout

    async def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Route an MCP JSON-RPC request through policy evaluation."""
        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params", {}) or {}

        if method == "tools/call":
            return await self._handle_tool_call(req_id, params, request)
        if method == "tools/list":
            return await self._handle_tools_list(req_id, request)
        if method == "resources/read":
            return await self._handle_resource_read(req_id, params, request)
        return await self._forward(request)

    # -- enforcement ---------------------------------------------------------

    def _evaluate(self, tool_name: str, arguments: dict) -> dict[str, Any]:
        """Evaluate one tool call against the loaded policies, in-process.

        Returns a verdict dict: {"action": ..., "policy_name": ..., "id": ...}.
        Fails CLOSED: on any evaluation error returns action="block" unless
        fail_open was explicitly set.
        """
        try:
            # policies.evaluate_for_span is the same primitive ingest uses.
            # Imported lazily so this module stays importable without the
            # full receiver app context (keeps unit tests light).
            from policies import evaluate_for_span

            attrs = {
                "strathon.tool.name": tool_name,
                "gen_ai.tool.name": tool_name,
                "strathon.tool.args": json.dumps(arguments, default=str),
                "strathon.source": "mcp_gateway",
            }
            matches = evaluate_for_span(self.policies, tool_name, attrs)
            if not matches:
                return {"action": "allow"}
            # list_policies returns priority-DESC, evaluate_for_span preserves
            # order, so the first match is the highest-priority action.
            top = matches[0]
            return {
                "action": top.get("action", "allow"),
                "policy_name": top.get("name", ""),
                "id": str(top.get("id", "")),
            }
        except Exception:
            logger.exception(
                "MCP policy evaluation failed for tool %s; %s",
                tool_name,
                "allowing (fail_open=True)" if self.fail_open else "BLOCKING (fail-closed)",
            )
            if self.fail_open:
                return {"action": "allow", "degraded": True}
            return {"action": "block", "policy_name": "_fail_closed",
                    "reason": "policy engine unavailable"}

    async def _handle_tool_call(
        self, req_id: Any, params: dict, original: dict,
    ) -> dict[str, Any]:
        tool_name = params.get("name", "unknown")
        arguments = params.get("arguments", {}) or {}

        if tool_name in self.blocked_tools:
            logger.warning("MCP tool call blocked (blocklist): %s", tool_name)
            return self._error(req_id, _ERR_BLOCKED,
                               f"Tool '{tool_name}' is blocked by Strathon policy")

        verdict = self._evaluate(tool_name, arguments)
        action = verdict.get("action", "allow")

        if action == "block":
            logger.warning("MCP tool call blocked (policy): %s -- %s",
                           tool_name, verdict.get("policy_name", ""))
            return self._error(
                req_id, _ERR_BLOCKED,
                f"Blocked by policy '{verdict.get('policy_name', 'unknown')}': "
                f"{verdict.get('reason', 'policy violation')}",
            )

        if action == "require_approval":
            logger.info("MCP tool call requires approval: %s", tool_name)
            return self._error(
                req_id, _ERR_APPROVAL_REQUIRED,
                f"Tool call '{tool_name}' requires human approval before it can run.",
            )

        # allow / throttle / steer / log all forward at the gateway layer.
        # (throttle and steer require in-process result substitution, which
        # the SDK adapters do; the gateway logs and forwards.)
        response = await self._forward(original)
        if self.scan_responses:
            response = self._scan_response(response)
        return response

    async def _handle_tools_list(self, req_id: Any, original: dict) -> dict[str, Any]:
        response = await self._forward(original)
        if self.blocked_tools and isinstance(response.get("result"), dict):
            tools = response["result"].get("tools", [])
            response["result"]["tools"] = [
                t for t in tools if t.get("name") not in self.blocked_tools
            ]
        return response

    async def _handle_resource_read(
        self, req_id: Any, params: dict, original: dict,
    ) -> dict[str, Any]:
        response = await self._forward(original)
        if self.scan_responses:
            response = self._scan_response(response)
        return response

    def _scan_response(self, response: dict) -> dict:
        """Redact leaked credentials from MCP response text content."""
        try:
            from credential_patterns import redact_credentials, scan_text
        except Exception:
            return response
        result = response.get("result")
        if not isinstance(result, dict):
            return response
        for item in result.get("content", []) or []:
            if item.get("type") == "text" and item.get("text"):
                if scan_text(item["text"]):
                    redacted, count = redact_credentials(item["text"])
                    item["text"] = redacted
                    logger.warning("Redacted %d credential(s) from MCP response", count)
        return response

    async def _forward(self, request: dict) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self.upstream_url, json=request, timeout=self.http_timeout,
                )
                return resp.json()
        except Exception as e:
            logger.exception("Failed to forward to upstream MCP server")
            return self._error(request.get("id"), _ERR_UPSTREAM,
                               f"Upstream MCP server error: {str(e)[:200]}")

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

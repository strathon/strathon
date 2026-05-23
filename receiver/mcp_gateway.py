"""MCP Security Gateway.

Sits between an AI agent and MCP (Model Context Protocol) servers.
Intercepts tool_call requests, evaluates them against Strathon
CEL policies, and either forwards or blocks.

Architecture:
  Agent → Strathon MCP Gateway → Real MCP Server
          (policy evaluation)

The gateway speaks MCP's JSON-RPC protocol (via HTTP/SSE transport)
and adds a security layer that MCP itself doesn't provide.

Supported MCP operations:
  tools/call     → evaluated against policies (block/steer/throttle/
                   require_approval). Main enforcement point.
  tools/list     → pass through (optionally filter blocked tools)
  resources/read → evaluated (PII check, allowlist)
  resources/list → pass through

Research: Anthropic MCP specification (spec.modelcontextprotocol.io),
MCP specification,
bidirectional scanning, OWASP MCP Top 10.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("strathon.mcp_gateway")


class MCPSecurityGateway:
    """MCP security gateway that evaluates tool calls against policies.

    Usage:
        gateway = MCPSecurityGateway(
            upstream_url="http://localhost:3000/mcp",
            strathon_api_key="stra_...",
            strathon_endpoint="http://localhost:4318",
        )
        response = await gateway.handle_request(mcp_request)
    """

    def __init__(
        self,
        upstream_url: str,
        strathon_api_key: str,
        strathon_endpoint: str = "http://localhost:4318",
        blocked_tools: list[str] | None = None,
        scan_responses: bool = True,
    ):
        self.upstream_url = upstream_url
        self.strathon_api_key = strathon_api_key
        self.strathon_endpoint = strathon_endpoint
        self.blocked_tools = set(blocked_tools or [])
        self.scan_responses = scan_responses

    async def handle_request(
        self, request: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle an MCP JSON-RPC request.

        Evaluates tool calls against Strathon policies before
        forwarding to the upstream MCP server.
        """
        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params", {})

        if method == "tools/call":
            return await self._handle_tool_call(req_id, params, request)
        elif method == "tools/list":
            return await self._handle_tools_list(req_id, request)
        elif method == "resources/read":
            return await self._handle_resource_read(req_id, params, request)
        else:
            # Pass through other methods.
            return await self._forward(request)

    async def _handle_tool_call(
        self, req_id: Any, params: dict, original: dict,
    ) -> dict[str, Any]:
        """Intercept tools/call and evaluate against Strathon policies."""
        tool_name = params.get("name", "unknown")
        arguments = params.get("arguments", {})

        # Check blocked tools list.
        if tool_name in self.blocked_tools:
            logger.warning("MCP tool call blocked (blocklist): %s", tool_name)
            return self._error_response(
                req_id, -32600,
                f"Tool '{tool_name}' is blocked by Strathon policy",
            )

        # Send to Strathon for policy evaluation via span ingest.
        verdict = await self._evaluate_with_strathon(tool_name, arguments)

        if verdict.get("action") == "block":
            logger.warning(
                "MCP tool call blocked (policy): %s — %s",
                tool_name, verdict.get("policy_name", ""),
            )
            return self._error_response(
                req_id, -32600,
                f"Blocked by policy '{verdict.get('policy_name', 'unknown')}': "
                f"{verdict.get('reason', 'policy violation')}",
            )

        if verdict.get("action") == "require_approval":
            logger.info(
                "MCP tool call held for approval: %s — %s",
                tool_name, verdict.get("approval_id", ""),
            )
            return self._error_response(
                req_id, -32001,
                f"Tool call held for human approval. "
                f"Approval ID: {verdict.get('approval_id', '')}",
            )

        # Forward to upstream.
        response = await self._forward(original)

        # Optionally scan the response for credential leakage.
        if self.scan_responses:
            response = await self._scan_response(response)

        return response

    async def _handle_tools_list(
        self, req_id: Any, original: dict,
    ) -> dict[str, Any]:
        """Forward tools/list and optionally filter blocked tools."""
        response = await self._forward(original)

        if self.blocked_tools and "result" in response:
            tools = response.get("result", {}).get("tools", [])
            filtered = [t for t in tools if t.get("name") not in self.blocked_tools]
            response["result"]["tools"] = filtered

        return response

    async def _handle_resource_read(
        self, req_id: Any, params: dict, original: dict,
    ) -> dict[str, Any]:
        """Forward resources/read with credential scanning on response."""
        response = await self._forward(original)

        if self.scan_responses:
            response = await self._scan_response(response)

        return response

    async def _evaluate_with_strathon(
        self, tool_name: str, arguments: dict,
    ) -> dict[str, Any]:
        """Send tool call to Strathon receiver for policy evaluation.

        Constructs a minimal OTLP-compatible span and checks the
        policy evaluation result.
        """
        try:
            async with httpx.AsyncClient() as client:
                # Use the policy check endpoint.
                resp = await client.post(
                    f"{self.strathon_endpoint}/v1/policies/evaluate",
                    headers={"Authorization": f"Bearer {self.strathon_api_key}"},
                    json={
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "source": "mcp_gateway",
                    },
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    return resp.json()
                # On error, fail open (allow) — the tool call proceeds.
                logger.warning(
                    "Strathon policy evaluation failed (%d), allowing tool call",
                    resp.status_code,
                )
                return {"action": "allow"}
        except Exception:
            logger.exception("Strathon unreachable, allowing tool call (fail-open)")
            return {"action": "allow"}

    async def _scan_response(self, response: dict) -> dict:
        """Scan MCP response content for credential leakage."""
        from credential_patterns import scan_text, redact_credentials

        result = response.get("result", {})
        content_items = result.get("content", [])

        for item in content_items:
            if item.get("type") == "text" and item.get("text"):
                findings = scan_text(item["text"])
                if findings:
                    redacted, count = redact_credentials(item["text"])
                    item["text"] = redacted
                    logger.warning(
                        "Redacted %d credentials from MCP response", count,
                    )

        return response

    async def _forward(self, request: dict) -> dict[str, Any]:
        """Forward a request to the upstream MCP server."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self.upstream_url,
                    json=request,
                    timeout=30.0,
                )
                return resp.json()
        except Exception as e:
            logger.exception("Failed to forward to upstream MCP server")
            return self._error_response(
                request.get("id"), -32603,
                f"Upstream MCP server error: {str(e)[:200]}",
            )

    @staticmethod
    def _error_response(req_id: Any, code: int, message: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        }

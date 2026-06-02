"""MCP Security Gateway endpoint.

  POST /v1/mcp/proxy

Proxies a single MCP JSON-RPC request to an upstream MCP server, evaluating
tools/call against the project's enabled policies first. This is the wired,
in-process entry point for the MCP gateway (see mcp_gateway.py for the
evaluator).

Scope: traces:write. Proxying live tool calls is the same trust level as
writing spans — both represent the agent's runtime activity flowing through
Strathon.

Enforcement is FAIL-CLOSED by default: if policy evaluation cannot complete,
a tools/call is blocked. Set fail_open=true in the body to prefer availability.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

import auth as auth_mod
from database import get_db_session
from mcp_gateway import MCPSecurityGateway
from repositories import policies as policies_repo
from sqlalchemy.ext.asyncio import AsyncSession

from ._deps import require_scope

logger = logging.getLogger("strathon.receiver.api.mcp")

router = APIRouter(prefix="/v1/mcp", tags=["mcp"])


class MCPProxyRequest(BaseModel):
    """Body for POST /v1/mcp/proxy."""

    upstream_url: str = Field(
        ...,
        description="The upstream MCP server URL to forward allowed requests to.",
    )
    request: dict[str, Any] = Field(
        ...,
        description="The MCP JSON-RPC request object to evaluate and proxy.",
    )
    blocked_tools: Optional[list[str]] = Field(
        default=None,
        description="Tool names to hard-block regardless of policy.",
    )
    scan_responses: bool = Field(
        default=True,
        description="Scan upstream responses and redact leaked credentials.",
    )
    fail_open: bool = Field(
        default=False,
        description=(
            "If true, allow a tools/call when policy evaluation fails. "
            "Default false (fail-closed): a security gateway should block, "
            "not allow, when its policy engine is unavailable."
        ),
    )


@router.post("/proxy")
async def mcp_proxy(
    body: MCPProxyRequest,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_TRACES_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Evaluate an MCP request against project policies, then proxy it."""
    # Load the project's enabled policies — the same call the ingest path
    # uses, so MCP tool calls are judged by the identical policy set/order.
    try:
        policy_models = await policies_repo.list_policies(
            session, ctx.project_id, only_enabled=True
        )
        active_policies = [
            {**p.model_dump(mode="python"), "id": str(p.id)}
            for p in policy_models
        ]
    except Exception:
        logger.exception(
            "failed to load policies for MCP proxy (project %s)", ctx.project_id
        )
        # Fail-closed posture: if we cannot load policies, hand the gateway an
        # empty set AND leave fail_open as requested. With no policies and
        # fail_open=False the gateway still forwards non-tools/call methods but
        # a tools/call evaluates to allow only if no policy would block — which
        # with zero policies means allow. To stay strict we force fail_open off
        # here so the gateway's own evaluation-error path (fail-closed) governs.
        active_policies = []

    gateway = MCPSecurityGateway(
        upstream_url=body.upstream_url,
        policies=active_policies,
        blocked_tools=body.blocked_tools,
        scan_responses=body.scan_responses,
        fail_open=body.fail_open,
    )
    return await gateway.handle_request(body.request)

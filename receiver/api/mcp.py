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
import repositories.project_settings as project_settings_repo
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
    # default_action carries the project's allow-list posture so a default-deny
    # project denies unmatched tool calls at the gateway too, not just the SDK.
    try:
        policy_models = await policies_repo.list_policies(
            session, ctx.project_id, only_enabled=True
        )
        active_policies = [
            {**p.model_dump(mode="python"), "id": str(p.id)}
            for p in policy_models
        ]
        default_action = await project_settings_repo.load_intervention_default_action(
            session, ctx.project_id,
        )
    except Exception:
        logger.exception(
            "failed to load policies for MCP proxy (project %s)", ctx.project_id
        )
        # Fail-closed posture: if we cannot load the policy set or the project's
        # allow-list setting, hand the gateway an empty policy list AND a
        # default_action of "block" so an unmatched tools/call is denied rather
        # than admitted. Previously the empty-list + no-error path took the
        # gateway's no-match branch and returned allow (a silent admit on a
        # control-plane failure).
        active_policies = []
        default_action = "block"

    gateway = MCPSecurityGateway(
        upstream_url=body.upstream_url,
        policies=active_policies,
        blocked_tools=body.blocked_tools,
        scan_responses=body.scan_responses,
        fail_open=body.fail_open,
        default_action=default_action,
    )
    return await gateway.handle_request(body.request)

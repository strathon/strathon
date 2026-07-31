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
a tools/call is blocked. The gateway is always fail-closed: if policy
# evaluation cannot complete, the call is blocked, not allowed.
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
        description=(
            "Additional tool names to hard-block, on top of policy. This can "
            "only ADD restrictions, never remove them, so it is safe to accept "
            "from the caller."
        ),
    )
    scan_responses: bool = Field(
        default=True,
        description="Scan upstream responses and redact leaked credentials.",
    )
    # fail_open is intentionally NOT a request field. It governs what happens
    # when policy evaluation cannot complete, which is a security decision that
    # must not be chosen by the policed party -- a caller could otherwise set it
    # to turn the evaluation-error path from block into allow. The gateway is
    # always constructed fail-closed (fail_open=False) below.


@router.post("/proxy")
async def mcp_proxy(
    body: MCPProxyRequest,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_TRACES_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Evaluate an MCP request against project policies, then proxy it."""
    # SSRF guard: upstream_url is caller-supplied and gets POSTed to server-side,
    # with the response returned to the caller. Without this, an agent holding a
    # traces:write key -- the key every instrumented agent already has -- could
    # point it at a cloud-metadata endpoint or internal service and exfiltrate
    # the response. Validation of upstream_url happens inside the gateway's
    # forward path, so it runs only when a call would actually be forwarded (a
    # policy-blocked call is never sent) and guards every outbound request; DNS
    # is resolved fresh at that point, which also defeats rebinding.

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
        # than admitted on a control-plane failure.
        active_policies = []
        default_action = "block"

    gateway = MCPSecurityGateway(
        upstream_url=body.upstream_url,
        policies=active_policies,
        blocked_tools=body.blocked_tools,
        scan_responses=body.scan_responses,
        fail_open=False,  # security decision, never caller-chosen
        default_action=default_action,
    )
    return await gateway.handle_request(body.request)

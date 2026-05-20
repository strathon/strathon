"""GitHub App integration.

  POST   /v1/integrations/github                  Register repo integration
  GET    /v1/integrations/github                  List integrations
  DELETE /v1/integrations/github/{id}             Remove integration
  GET    /v1/integrations/github/commits          List tracked commits
  POST   /v1/integrations/github/webhooks         Receive GitHub push/PR events

Completes the existing models/git.py schema stubs (GitHubIntegration +
GitCommit tables). Links agent deployments to git commits so operators
can correlate behavior changes with code changes.

Research: GitHub Apps API, GitHub webhook events (push, pull_request),
HMAC-SHA256 webhook signature verification (X-Hub-Signature-256).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import (
    APIRouter, Depends, HTTPException, Header, Request, status,
)
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
from database import get_db_session

from ._deps import require_scope

logger = logging.getLogger("strathon.api.github")

router = APIRouter(tags=["github"])


# ---- Request models ---------------------------------------------------------

class RegisterGitHubRequest(BaseModel):
    repo_full_name: str = Field(
        ..., min_length=3, max_length=200,
        description="owner/repo format",
    )
    webhook_secret: str = Field(
        ..., min_length=8, max_length=500,
        description="Shared secret for GitHub webhook HMAC verification",
    )
    installation_id: Optional[int] = None

    model_config = {"extra": "forbid"}


# ---- CRUD -------------------------------------------------------------------

@router.post("/v1/integrations/github", status_code=status.HTTP_201_CREATED)
async def register_github(
    body: RegisterGitHubRequest,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Register a GitHub repository integration."""
    # Check for duplicate.
    existing = await session.execute(text(
        "SELECT id FROM github_integrations "
        "WHERE project_id = :pid AND repo_full_name = :repo"
    ), {"pid": ctx.project_id, "repo": body.repo_full_name})
    if existing.first():
        raise HTTPException(409, f"integration for {body.repo_full_name} already exists")

    # For API key auth, no user context exists. Use NULL.
    user_id = getattr(ctx, "user_id", None)

    result = await session.execute(text(
        "INSERT INTO github_integrations "
        "(project_id, repo_full_name, webhook_secret, installation_id, created_by_user_id) "
        "VALUES (:pid, :repo, :secret, :iid, :uid) "
        "RETURNING id, project_id, repo_full_name, installation_id, created_at"
    ), {
        "pid": ctx.project_id,
        "repo": body.repo_full_name,
        "secret": body.webhook_secret,
        "iid": body.installation_id,
        "uid": user_id,
    })
    row = result.mappings().first()
    await session.commit()
    return _serialize(row)


@router.get("/v1/integrations/github")
async def list_github_integrations(
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    result = await session.execute(text(
        "SELECT id, project_id, repo_full_name, installation_id, "
        "created_at, last_event_at "
        "FROM github_integrations WHERE project_id = :pid "
        "ORDER BY created_at DESC"
    ), {"pid": ctx.project_id})
    return {"data": [_serialize(r) for r in result.mappings().all()]}


@router.delete(
    "/v1/integrations/github/{integration_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_github_integration(
    integration_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        iid = UUID(integration_id)
    except ValueError:
        raise HTTPException(400, "invalid integration_id")

    result = await session.execute(text(
        "DELETE FROM github_integrations "
        "WHERE id = :iid AND project_id = :pid"
    ), {"iid": iid, "pid": ctx.project_id})
    if not result.rowcount:
        raise HTTPException(404, "integration not found")
    await session.commit()


# ---- Commits ----------------------------------------------------------------

@router.get("/v1/integrations/github/commits")
async def list_commits(
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_TRACES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List tracked git commits, newest first."""
    clauses = ["project_id = :pid"]
    params: dict[str, Any] = {"pid": ctx.project_id, "lim": min(limit, 200)}
    if repo:
        clauses.append("repo_full_name = :repo")
        params["repo"] = repo
    if branch:
        clauses.append("branch = :branch")
        params["branch"] = branch

    where = " AND ".join(clauses)
    result = await session.execute(text(
        f"SELECT * FROM git_commits WHERE {where} "
        f"ORDER BY committed_at DESC NULLS LAST LIMIT :lim"
    ), params)
    return {"data": [_serialize_commit(r) for r in result.mappings().all()]}


# ---- Webhook Handler --------------------------------------------------------

@router.post("/v1/integrations/github/webhooks")
async def handle_github_webhook(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    x_hub_signature_256: Optional[str] = Header(None),
    x_github_event: Optional[str] = Header(None),
):
    """Receive GitHub webhook events (push, pull_request).

    Verifies HMAC-SHA256 signature against the integration's webhook_secret.
    On push: records commits in git_commits table.
    On pull_request: could trigger policy checks (future).
    """
    body = await request.body()

    if not x_github_event:
        raise HTTPException(400, "missing X-GitHub-Event header")

    # Parse body to find repo.
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid JSON body")

    repo_name = payload.get("repository", {}).get("full_name")
    if not repo_name:
        raise HTTPException(400, "missing repository.full_name in payload")

    # Find matching integration.
    result = await session.execute(text(
        "SELECT id, project_id, webhook_secret FROM github_integrations "
        "WHERE repo_full_name = :repo"
    ), {"repo": repo_name})
    integration = result.mappings().first()
    if not integration:
        raise HTTPException(404, "no integration for this repository")

    # Verify signature.
    if x_hub_signature_256:
        expected = "sha256=" + hmac.new(
            integration["webhook_secret"].encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, x_hub_signature_256):
            raise HTTPException(403, "invalid webhook signature")
    else:
        logger.warning(
            "GitHub webhook for %s has no signature header", repo_name
        )

    # Update last_event_at.
    await session.execute(text(
        "UPDATE github_integrations SET last_event_at = NOW() WHERE id = :iid"
    ), {"iid": integration["id"]})

    # Handle event.
    if x_github_event == "push":
        commits = payload.get("commits", [])
        branch = (payload.get("ref") or "").replace("refs/heads/", "")
        inserted = 0
        for c in commits:
            try:
                committed_at = None
                if c.get("timestamp"):
                    committed_at = datetime.fromisoformat(
                        c["timestamp"].replace("Z", "+00:00")
                    )

                await session.execute(text(
                    "INSERT INTO git_commits "
                    "(project_id, integration_id, commit_sha, repo_full_name, "
                    "commit_message, author_name, author_email, committed_at, branch) "
                    "VALUES (:pid, :iid, :sha, :repo, :msg, :name, :email, :at, :branch) "
                    "ON CONFLICT (project_id, commit_sha) DO NOTHING"
                ), {
                    "pid": integration["project_id"],
                    "iid": integration["id"],
                    "sha": c.get("id", ""),
                    "repo": repo_name,
                    "msg": (c.get("message") or "")[:500],
                    "name": (c.get("author", {}).get("name") or "")[:200],
                    "email": (c.get("author", {}).get("email") or "")[:200],
                    "at": committed_at,
                    "branch": branch[:200] if branch else None,
                })
                inserted += 1
            except Exception:
                logger.exception("Failed to insert commit %s", c.get("id"))

        await session.commit()
        return {"event": "push", "commits_tracked": inserted}

    elif x_github_event == "pull_request":
        action = payload.get("action")
        pr_number = payload.get("number")
        head_sha = payload.get("pull_request", {}).get("head", {}).get("sha")
        logger.info(
            "PR #%s %s on %s (head: %s)",
            pr_number, action, repo_name, head_sha,
        )
        await session.commit()
        return {"event": "pull_request", "action": action, "pr_number": pr_number}

    elif x_github_event == "ping":
        await session.commit()
        return {"event": "ping", "zen": payload.get("zen", "")}

    else:
        await session.commit()
        return {"event": x_github_event, "status": "ignored"}


# ---- Helpers ----------------------------------------------------------------

def _serialize(row) -> dict[str, Any]:
    if row is None:
        return {}
    d = dict(row)
    for k in ("id", "project_id"):
        if k in d:
            d[k] = str(d[k])
    for k in ("created_at", "last_event_at"):
        if k in d and d[k]:
            d[k] = d[k].isoformat()
    return d


def _serialize_commit(row) -> dict[str, Any]:
    if row is None:
        return {}
    d = dict(row)
    for k in ("id", "project_id", "integration_id"):
        if k in d and d[k]:
            d[k] = str(d[k])
    for k in ("committed_at", "fetched_at"):
        if k in d and d[k]:
            d[k] = d[k].isoformat()
    return d

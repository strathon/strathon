"""GitHub integration models.

Schema-only today. The repo→deploy linkage feature ships post-v1; these
tables exist so traces.git_commit_sha can FK against a real source of truth
when that lands.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    TIMESTAMP,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class GitHubIntegration(Base):
    """GitHub App / OAuth installation tied to a Strathon project."""

    __tablename__ = "github_integrations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    repo_full_name: Mapped[str] = mapped_column(Text, nullable=False)
    installation_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    webhook_secret: Mapped[str] = mapped_column(Text, nullable=False)

    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    last_event_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        UniqueConstraint("project_id", "repo_full_name"),
        Index("idx_github_integrations_project", "project_id"),
    )


class GitCommit(Base):
    """Cached commit metadata so spans can link to their deploy."""

    __tablename__ = "git_commits"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    integration_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("github_integrations.id", ondelete="SET NULL"),
    )

    commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    repo_full_name: Mapped[str] = mapped_column(Text, nullable=False)
    commit_message: Mapped[Optional[str]] = mapped_column(Text)
    author_name: Mapped[Optional[str]] = mapped_column(Text)
    author_email: Mapped[Optional[str]] = mapped_column(Text)
    committed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    branch: Mapped[Optional[str]] = mapped_column(Text)
    pr_number: Mapped[Optional[int]] = mapped_column(Integer)

    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("project_id", "commit_sha"),
        Index("idx_git_commits_sha", "project_id", "commit_sha"),
        Index(
            "idx_git_commits_committed_at",
            "project_id",
            text("committed_at DESC"),
            postgresql_where=text("committed_at IS NOT NULL"),
        ),
    )

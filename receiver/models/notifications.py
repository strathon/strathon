"""Notification channel model.

Operators configure where alerts, incidents, and approval requests are
delivered (Slack, Discord, GitHub, generic webhook). Mirrors the schema
created in the notification_channels migration.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ARRAY, Boolean, CheckConstraint, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class NotificationChannel(Base, TimestampMixin):
    """A configured delivery target for notifications."""

    __tablename__ = "notification_channels"

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
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'"),
    )
    events: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'"),
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"),
    )

    __table_args__ = (
        CheckConstraint(
            "channel_type IN ('slack', 'discord', 'github', 'webhook')",
            name="notification_channels_channel_type_check",
        ),
        Index(
            "idx_notification_channels_project",
            "project_id", "enabled",
        ),
    )

"""Notification channels for Slack, Discord, GitHub, and generic webhooks.

Operators configure where alerts, incidents, and approval requests
are delivered. Each channel has a type, config (URL, token, etc.),
and an event filter controlling which event types are sent.

Revision ID: 023
Revises: 022
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "023"
down_revision: Union[str, Sequence[str], None] = "022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE notification_channels (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    channel_type TEXT NOT NULL
        CHECK (channel_type IN ('slack', 'discord', 'github', 'webhook')),
    name TEXT NOT NULL,
    config JSONB NOT NULL DEFAULT '{}',
    events TEXT[] NOT NULL DEFAULT '{}',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
""")
    op.execute("""
CREATE INDEX idx_notification_channels_project
    ON notification_channels (project_id, enabled)
""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS notification_channels CASCADE")

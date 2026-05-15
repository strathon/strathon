"""ORM models for the Strathon receiver.

Importing this package imports every model class. That matters because
Alembic's `target_metadata = Base.metadata` only knows about a model
class if it has been imported at least once — Python registers the class
with the DeclarativeBase metadata at class-definition time. So Alembic's
env.py imports this package, which imports every model file, which
registers every table.

Do not move model classes to subpackages without re-exporting them here.
"""

from .base import Base, TimestampMixin
from .core import ApiKey, Project, ProjectSettings, Session
from .git import GitCommit, GitHubIntegration
from .identity import ProjectMember, User
from .intervention import Budget, HaltState, InterventionLog
from .policies import Policy, PolicyMatch
from .traces import Span, SpanEvent, SpanLink, Trace
from .webhooks import WebhookDelivery, WebhookSigningKey

__all__ = [
    # Base
    "Base",
    "TimestampMixin",
    # Core
    "Project",
    "ApiKey",
    "Session",
    "ProjectSettings",
    # Identity
    "User",
    "ProjectMember",
    # Traces
    "Trace",
    "Span",
    "SpanEvent",
    "SpanLink",
    # Policies
    "Policy",
    "PolicyMatch",
    # Intervention
    "Budget",
    "HaltState",
    "InterventionLog",
    # Git
    "GitHubIntegration",
    "GitCommit",
    # Webhooks
    "WebhookDelivery",
    "WebhookSigningKey",
]

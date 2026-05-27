"""Role-based access control definitions for Strathon.

Four fixed roles in the open-source tier. Custom roles live in ee/.

Role hierarchy (higher can manage lower):
    owner > admin > operator > viewer

Role → scope mapping is the bridge between RBAC (what role a user has) and
the existing scope system (what an API key can do). Session-based auth
resolves the user's role via project_members, then maps it to scopes.
API keys bypass the role system and use explicit scopes as before.

Four fixed roles (owner/admin/operator/viewer) with hierarchical
scope mapping, following industry-standard patterns for multi-tenant
SaaS platforms.
"""

from __future__ import annotations

from typing import Literal

from auth import (
    SCOPE_API_KEYS_READ,
    SCOPE_API_KEYS_WRITE,
    SCOPE_AUDIT_ADMIN,
    SCOPE_AUDIT_READ,
    SCOPE_AUDIT_WRITE,
    SCOPE_BUDGETS_READ,
    SCOPE_BUDGETS_WRITE,
    SCOPE_HALTS_READ,
    SCOPE_HALTS_WRITE,
    SCOPE_MODEL_PRICES_READ,
    SCOPE_MODEL_PRICES_WRITE,
    SCOPE_POLICIES_READ,
    SCOPE_POLICIES_WRITE,
    SCOPE_PROJECT_SETTINGS_READ,
    SCOPE_PROJECT_SETTINGS_WRITE,
    SCOPE_TRACES_READ,
    SCOPE_TRACES_WRITE,
    SCOPE_WEBHOOK_DELIVERIES_READ,
    SCOPE_WEBHOOK_DELIVERIES_WRITE,
    SCOPE_WEBHOOK_SIGNING_KEYS_READ,
    SCOPE_WEBHOOK_SIGNING_KEYS_WRITE,
    SCOPE_WILDCARD,
)


# ---- Role type -----------------------------------------------------------

Role = Literal["owner", "admin", "operator", "viewer"]
VALID_ROLES: frozenset[str] = frozenset({"owner", "admin", "operator", "viewer"})

# ---- Role hierarchy (for "can this role manage that role?" checks) --------

ROLE_RANK: dict[str, int] = {
    "owner": 40,
    "admin": 30,
    "operator": 20,
    "viewer": 10,
}


def can_manage_role(actor_role: str, target_role: str) -> bool:
    """Return True if actor_role outranks target_role.

    Used for membership management: you can only invite/change/remove
    users whose role is strictly below yours. Owners can manage admins,
    admins can manage operators and viewers, etc.
    """
    return ROLE_RANK.get(actor_role, 0) > ROLE_RANK.get(target_role, 0)


# ---- Role → scope mapping ------------------------------------------------
#
# These are the scopes a session-authenticated user receives based on their
# project membership role. The mapping is fixed in the open-source tier.
#
# SDK API keys continue to use explicit scopes (the existing system) and
# are not affected by these mappings.

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "owner": frozenset({SCOPE_WILDCARD}),

    "admin": frozenset({
        # Everything except SCOPE_WILDCARD and SCOPE_PROJECTS_MANAGE (no project deletion)
        SCOPE_TRACES_WRITE,
        SCOPE_TRACES_READ,
        SCOPE_POLICIES_READ,
        SCOPE_POLICIES_WRITE,
        SCOPE_API_KEYS_READ,
        SCOPE_API_KEYS_WRITE,
        SCOPE_WEBHOOK_SIGNING_KEYS_READ,
        SCOPE_WEBHOOK_SIGNING_KEYS_WRITE,
        SCOPE_WEBHOOK_DELIVERIES_READ,
        SCOPE_WEBHOOK_DELIVERIES_WRITE,
        SCOPE_HALTS_READ,
        SCOPE_HALTS_WRITE,
        SCOPE_BUDGETS_READ,
        SCOPE_BUDGETS_WRITE,
        SCOPE_MODEL_PRICES_READ,
        SCOPE_MODEL_PRICES_WRITE,
        SCOPE_PROJECT_SETTINGS_READ,
        SCOPE_PROJECT_SETTINGS_WRITE,
        SCOPE_AUDIT_READ,
        SCOPE_AUDIT_WRITE,
        SCOPE_AUDIT_ADMIN,
    }),

    "operator": frozenset({
        SCOPE_TRACES_READ,
        SCOPE_TRACES_WRITE,
        SCOPE_POLICIES_READ,
        SCOPE_POLICIES_WRITE,
        SCOPE_API_KEYS_READ,
        SCOPE_HALTS_READ,
        SCOPE_HALTS_WRITE,
        SCOPE_BUDGETS_READ,
        SCOPE_BUDGETS_WRITE,
        SCOPE_MODEL_PRICES_READ,
        SCOPE_MODEL_PRICES_WRITE,
        SCOPE_WEBHOOK_SIGNING_KEYS_READ,
        SCOPE_WEBHOOK_SIGNING_KEYS_WRITE,
        SCOPE_WEBHOOK_DELIVERIES_READ,
        SCOPE_WEBHOOK_DELIVERIES_WRITE,
        SCOPE_PROJECT_SETTINGS_READ,
        SCOPE_PROJECT_SETTINGS_WRITE,
        SCOPE_AUDIT_READ,
    }),

    "viewer": frozenset({
        SCOPE_TRACES_READ,
        SCOPE_POLICIES_READ,
        SCOPE_HALTS_READ,
        SCOPE_BUDGETS_READ,
        SCOPE_MODEL_PRICES_READ,
        SCOPE_WEBHOOK_DELIVERIES_READ,
        SCOPE_WEBHOOK_SIGNING_KEYS_READ,
        SCOPE_PROJECT_SETTINGS_READ,
        SCOPE_AUDIT_READ,
    }),
}


def role_has_scope(role: str, required_scope: str) -> bool:
    """Check if a role includes the required scope."""
    scopes = ROLE_SCOPES.get(role, frozenset())
    return SCOPE_WILDCARD in scopes or required_scope in scopes


# ---- Scope for membership management (not a resource scope) --------------

SCOPE_MEMBERS_READ = "members:read"
SCOPE_MEMBERS_WRITE = "members:write"


__all__ = [
    "ROLE_RANK",
    "ROLE_SCOPES",
    "Role",
    "SCOPE_MEMBERS_READ",
    "SCOPE_MEMBERS_WRITE",
    "VALID_ROLES",
    "can_manage_role",
    "role_has_scope",
]

"""Controlled vocabulary of audit action names.

Every audit event records an ``action`` and an ``action_category``.
Categories are coarse-grained groupings (one per resource family);
actions are specific verbs. Both are free-form strings at the
database level, but every emission site in receiver code uses one
of the constants below so the set is enumerable.

Naming convention:

- Categories are lowercase, singular nouns (``policy``, ``halt``).
- Actions are ``<category>.<verb>``; verbs use lowercase past-tense
  or imperative forms (``create``, ``update``, ``delete``, ``issue``,
  ``revoke``, ``rotate``, ``replay``).

Reserved action ``audit.read`` is emitted when an operator queries
the audit log — the audit-of-the-audit-log control from research
§10 anti-pattern #11.
"""

from __future__ import annotations

from typing import Final


# --- Categories ----------------------------------------------------------

CATEGORY_POLICY: Final[str] = "policy"
CATEGORY_HALT: Final[str] = "halt"
CATEGORY_BUDGET: Final[str] = "budget"
CATEGORY_API_KEY: Final[str] = "api_key"
CATEGORY_PROJECT_SETTINGS: Final[str] = "project_settings"
CATEGORY_WEBHOOK_SIGNING_KEY: Final[str] = "webhook_signing_key"
CATEGORY_WEBHOOK_DELIVERY: Final[str] = "webhook_delivery"
CATEGORY_MODEL_PRICE: Final[str] = "model_price"
CATEGORY_AUDIT_STREAM: Final[str] = "audit_stream"
CATEGORY_AUDIT: Final[str] = "audit"


KNOWN_CATEGORIES: frozenset[str] = frozenset({
    CATEGORY_POLICY,
    CATEGORY_HALT,
    CATEGORY_BUDGET,
    CATEGORY_API_KEY,
    CATEGORY_PROJECT_SETTINGS,
    CATEGORY_WEBHOOK_SIGNING_KEY,
    CATEGORY_WEBHOOK_DELIVERY,
    CATEGORY_MODEL_PRICE,
    CATEGORY_AUDIT_STREAM,
    CATEGORY_AUDIT,
})


# --- Specific actions ----------------------------------------------------

POLICY_CREATE: Final[str] = "policy.create"
POLICY_UPDATE: Final[str] = "policy.update"
POLICY_DELETE: Final[str] = "policy.delete"

HALT_ISSUE: Final[str] = "halt.issue"
HALT_CLEAR: Final[str] = "halt.clear"

BUDGET_CREATE: Final[str] = "budget.create"
BUDGET_UPDATE: Final[str] = "budget.update"
BUDGET_DELETE: Final[str] = "budget.delete"

API_KEY_CREATE: Final[str] = "api_key.create"
API_KEY_REVOKE: Final[str] = "api_key.revoke"
API_KEY_ROTATE: Final[str] = "api_key.rotate"
API_KEY_UPDATE: Final[str] = "api_key.update"

PROJECT_SETTINGS_UPDATE: Final[str] = "project_settings.update"

WEBHOOK_SIGNING_KEY_CREATE: Final[str] = "webhook_signing_key.create"
WEBHOOK_SIGNING_KEY_REVOKE: Final[str] = "webhook_signing_key.revoke"

WEBHOOK_DELIVERY_REPLAY: Final[str] = "webhook_delivery.replay"

MODEL_PRICE_SET: Final[str] = "model_price.set"
MODEL_PRICE_DELETE: Final[str] = "model_price.delete"

AUDIT_STREAM_CREATE: Final[str] = "audit_stream.create"
AUDIT_STREAM_DELETE: Final[str] = "audit_stream.delete"

AUDIT_READ: Final[str] = "audit.read"
AUDIT_EXPORT: Final[str] = "audit.export"


# --- Outcomes ------------------------------------------------------------

OUTCOME_ALLOW: Final[str] = "allow"
OUTCOME_DENY: Final[str] = "deny"
OUTCOME_ERROR: Final[str] = "error"
OUTCOME_PARTIAL: Final[str] = "partial"


# --- Actor types ---------------------------------------------------------

ACTOR_HUMAN: Final[str] = "human"
ACTOR_SERVICE_ACCOUNT: Final[str] = "service_account"
ACTOR_AGENT: Final[str] = "agent"
ACTOR_SYSTEM: Final[str] = "system"
ACTOR_ANONYMOUS: Final[str] = "anonymous"

"""Per-field sensitivity rules for audit event payloads.

Three handling strategies, chosen per field via a static rule table:

- ``exclude``: the field is removed entirely from ``before_state`` /
  ``after_state``. Use for raw secrets that have no audit value —
  e.g., the actual bytes of an API key.
- ``hmac``: the field's value is replaced with
  ``hmac-sha256:<hex>`` computed under the audit HMAC key. Use for
  identifiers we may need to correlate but whose plaintext is
  sensitive — e.g., the value of an API token we want to match
  against a leaked-secret report.
- ``redact``: the field's value is replaced with the literal string
  ``[REDACTED]``. Use for fields where existence matters but value
  doesn't — e.g., session passwords that should never be logged
  even hashed.

The rules are intentionally hardcoded in this module rather than
sourced from a database table; the current default is fixed and
conservative. Per-tenant configurability (backed by
``audit.field_rules``) is a planned extension.

Tests live in ``tests/test_audit_redaction.py``.
"""

from __future__ import annotations

import hmac
from hashlib import sha256
from typing import Any, Literal


Strategy = Literal["exclude", "hmac", "redact"]


# Field paths are dotted: ``api_key.value`` matches the ``value`` key
# inside a top-level ``api_key`` dict. Globs are not supported; rules
# must enumerate sensitive paths explicitly.
_RULES: dict[str, Strategy] = {
    # API keys: the raw key value must never appear in audit. We
    # store the prefix and an HMAC of the value separately at the
    # emit site, so the entire `value` field is excluded here.
    "value": "exclude",
    "key": "exclude",
    "api_key": "exclude",
    "secret": "exclude",
    "password": "redact",
    "password_hash": "redact",
    "token": "exclude",
    # Webhook signing key plaintext (only shown once at creation).
    "signing_key": "exclude",
    "signing_secret": "exclude",
    # Session tokens.
    "session_token": "exclude",
    "refresh_token": "exclude",
    # IDs we may want to correlate against external reports.
    "stripe_customer_id": "hmac",
    "external_user_id": "hmac",
}


def redact_state(
    state: Any,
    hmac_key: bytes,
) -> Any:
    """Recursively apply sensitivity rules to a state dict.

    Returns a new structure with the same shape; the input is not
    mutated. Non-dict / non-list values pass through unchanged. List
    elements are processed individually.
    """
    if state is None:
        return None
    if isinstance(state, dict):
        out: dict[str, Any] = {}
        for k, v in state.items():
            strategy = _RULES.get(k)
            if strategy == "exclude":
                continue
            if strategy == "redact":
                out[k] = "[REDACTED]"
                continue
            if strategy == "hmac":
                out[k] = _hmac_string(v, hmac_key)
                continue
            out[k] = redact_state(v, hmac_key)
        return out
    if isinstance(state, list):
        return [redact_state(item, hmac_key) for item in state]
    return state


def _hmac_string(value: Any, key: bytes) -> str:
    """Return ``hmac-sha256:<hex>`` over the string form of ``value``."""
    mac = hmac.new(key, str(value).encode("utf-8"), sha256)
    return f"hmac-sha256:{mac.hexdigest()}"


def hmac_value(value: str, key: bytes) -> str:
    """Public helper to compute an audit-safe correlation HMAC.

    Used at emit sites that need to record an HMAC of a sensitive
    value separately (e.g., ``api_key_hmac`` alongside the prefix
    and excluded plaintext).
    """
    return _hmac_string(value, key)

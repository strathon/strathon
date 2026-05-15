"""In-process cache of plaintext signing secrets for active webhook signing keys.

The database stores only SHA-256 hashes of signing-key plaintexts (see
``models/webhooks.py:WebhookSigningKey``). Plaintexts are not recoverable
from the database — by design, to limit the blast radius of a DB compromise.

But the receiver needs the plaintext to compute outbound HMAC signatures.
This module is that bridge: when an operator creates or rotates a signing
key, the plaintext is pushed into this in-memory keystore alongside the
hash being persisted. The actor reads from the keystore when assembling
the webhook-signature header.

Implications operators should know
==================================

* If the receiver process restarts, the keystore is empty. Until the
  operator re-supplies the plaintext (via PUT /v1/webhook_signing_keys
  with the plaintext as a body, or by rotating to a new key), outbound
  webhooks go unsigned. The webhook-id and webhook-timestamp headers
  still go out; the consumer's verification step rejects them, which
  is the correct failure mode for "the signing key is missing." We
  don't pretend signatures are valid when they aren't.

* This is the same trade-off Stripe makes. Their dashboard "Reveal
  signing secret" is, internally, just looking up the plaintext from a
  keystore Stripe owns — but they (a Stripe-sized organization) take on
  the cost of running that keystore reliably. For Strathon, an
  open-source firewall users self-host, asking them to run another
  state store seems wrong. The plaintext cache lives in receiver memory;
  if you need durable signing across restarts, mount your own KMS-backed
  secret and inject it on startup (see deploy docs).

Threading
=========

Read-heavy: every webhook delivery does one get_active_secrets() call.
Write-light: only on key creation/revocation/restart-restore. Using a
plain dict + lock; this is not a bottleneck.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Dict, List

logger = logging.getLogger("strathon.receiver.webhooks.keystore")


# project_id -> list of plaintext secrets currently considered active.
# Multiple entries support rotation: both keys sign for the rotation window.
_secrets_by_project: Dict[uuid.UUID, List[str]] = {}
_lock = threading.RLock()


def remember_secret(project_id: uuid.UUID, plaintext: str) -> None:
    """Add a plaintext to the keystore for a project.

    Called when:
      * An operator creates a new signing key (POST endpoint).
      * The receiver process restarts and an operator re-supplies the
        plaintext via PUT.

    Idempotent: adding the same plaintext twice is a no-op.
    """
    if not plaintext:
        return
    with _lock:
        secrets = _secrets_by_project.setdefault(project_id, [])
        if plaintext not in secrets:
            secrets.append(plaintext)


def forget_secret(project_id: uuid.UUID, plaintext: str) -> None:
    """Remove a plaintext (revoked key).

    Called when the operator revokes a signing key via DELETE. After
    this, deliveries no longer carry a signature derived from this
    secret. Other active secrets keep working.
    """
    if not plaintext:
        return
    with _lock:
        secrets = _secrets_by_project.get(project_id)
        if secrets and plaintext in secrets:
            secrets.remove(plaintext)
            if not secrets:
                # Tidy: don't keep an empty list around as evidence of
                # past keys. Absent dict entry means "no active keys."
                _secrets_by_project.pop(project_id, None)


def get_active_secrets(project_id: uuid.UUID) -> List[str]:
    """Snapshot of currently active plaintext secrets for the project.

    Returns a shallow copy so callers can iterate without holding the
    lock. The list order is creation order so signatures appear in a
    deterministic sequence in the multi-sig header — useful for debugging
    rotation issues.
    """
    with _lock:
        secrets = _secrets_by_project.get(project_id)
        return list(secrets) if secrets else []


def reset_for_testing() -> None:
    """Wipe the keystore. Tests use this between cases."""
    with _lock:
        _secrets_by_project.clear()


__all__ = [
    "forget_secret",
    "get_active_secrets",
    "remember_secret",
    "reset_for_testing",
]

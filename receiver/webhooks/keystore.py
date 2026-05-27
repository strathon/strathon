"""In-process cache of plaintext signing secrets, keyed by signing-key id.

The database stores only SHA-256 hashes of signing-key plaintexts (see
``models/webhooks.py:WebhookSigningKey``). Plaintexts are not recoverable
from the database — by design, to limit the blast radius of a DB compromise.

But the receiver needs the plaintext to compute outbound HMAC signatures.
This module is that bridge: when an operator creates a signing key via
POST /v1/webhook_signing_keys, the plaintext is pushed into the keystore
alongside the hash being persisted. The actor reads ``get_active_secrets``
when assembling the webhook-signature header.

Keying
======

Each plaintext is held under the (project_id, key_id) pair that
identifies it in the webhook_signing_keys table. Keying by id rather
than by plaintext value means revocation is a clean O(1) drop: the
revoke endpoint passes the id (which it knows) and the keystore drops
that entry, no plaintext required.

``get_active_secrets(project_id)`` returns just the plaintexts (the
caller is the signing layer, which doesn't care about ids). Ordering
is insertion order so multi-signature headers are deterministic for
debugging.

Operator implications
=====================

* If the receiver process restarts, the keystore is empty. Until the
  operator re-supplies the plaintexts via STRATHON_WEBHOOK_SIGNING_SECRETS
  (read at boot — see main.py lifespan) or creates fresh keys, outbound
  webhooks go unsigned. The webhook-id and webhook-timestamp headers
  still go out; the consumer's verification step rejects them, which is
  the correct failure mode for "the signing key is missing." We do not
  pretend signatures are valid when they aren't.

* This is the same trade-off Stripe makes. Their dashboard "Reveal
  signing secret" is, internally, looking up the plaintext from a
  keystore Stripe owns — but they (a Stripe-sized organization) take
  on the cost of running that keystore reliably. For Strathon, an
  open-source firewall users self-host, asking them to run another
  state store seems wrong. The plaintext cache lives in receiver memory;
  if you need durable signing across restarts, mount your own
  KMS-backed secret and inject it on startup via the env var.

Threading
=========

Read-heavy: every webhook delivery does one get_active_secrets() call.
Write-light: only on key creation/revocation/restart-restore. Using a
plain dict-of-dicts + lock; this is not a bottleneck.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Dict, List

logger = logging.getLogger("strathon.receiver.webhooks.keystore")


# project_id -> { key_id -> plaintext }
_secrets: Dict[uuid.UUID, Dict[uuid.UUID, str]] = {}
_lock = threading.RLock()


def remember_secret(
    project_id: uuid.UUID,
    plaintext: str,
    key_id: uuid.UUID | None = None,
) -> None:
    """Add a plaintext to the keystore for a (project_id, key_id) pair.

    Called from:
      * POST /v1/webhook_signing_keys after a successful row insert
      * The boot-time restore path that walks STRATHON_WEBHOOK_SIGNING_SECRETS

    key_id is required for clean revocation later; if the caller does
    not know the id (extreme edge case — synthetic test fixture), a
    fresh UUID is generated so the entry has a stable key in the
    keystore even though no DB row points at it. Production callers
    always supply the id.

    Idempotent: re-adding the same (project, id, plaintext) is a no-op.
    Re-adding with the same id but a different plaintext replaces — the
    operator presumably rotated and we trust the latest write.
    """
    if not plaintext:
        return
    kid = key_id if key_id is not None else uuid.uuid4()
    with _lock:
        bucket = _secrets.setdefault(project_id, {})
        bucket[kid] = plaintext


def forget_secret_by_id(project_id: uuid.UUID, key_id: uuid.UUID) -> None:
    """Drop the plaintext for a specific signing key.

    Called from DELETE /v1/webhook_signing_keys/{id} after the revoke
    row update succeeds. Other plaintexts for the same project keep
    signing future deliveries.

    Idempotent: a missing entry is a no-op.
    """
    with _lock:
        bucket = _secrets.get(project_id)
        if not bucket:
            return
        bucket.pop(key_id, None)
        if not bucket:
            _secrets.pop(project_id, None)


def get_active_secrets(project_id: uuid.UUID) -> List[str]:
    """Return all plaintext secrets currently active for the project.

    Returns a list copy so callers can iterate without holding the
    lock. Order is dict-iteration order (insertion order in 3.7+),
    which is creation order — useful so multi-signature headers are
    deterministic for debugging.
    """
    with _lock:
        bucket = _secrets.get(project_id)
        if not bucket:
            return []
        return list(bucket.values())


def reset_for_testing() -> None:
    """Wipe the keystore. Tests use this between cases."""
    with _lock:
        _secrets.clear()


__all__ = [
    "forget_secret_by_id",
    "get_active_secrets",
    "remember_secret",
    "reset_for_testing",
]

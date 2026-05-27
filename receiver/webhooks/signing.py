"""Standard Webhooks-compliant HMAC signing for outbound alerts.

This module wraps the ``standardwebhooks`` reference library with two
small pieces of project-local infrastructure: signing-key creation
(plaintext returned once, hash persisted) and rotation-aware multi-key
signature emission.

Why follow Standard Webhooks
----------------------------

We deliberately chose the Standard Webhooks specification (HMAC-SHA256
over ``{webhook-id}.{webhook-timestamp}.{body}`` with three headers
``webhook-id``, ``webhook-timestamp``, ``webhook-signature``) because
the convergence is real: OpenAI, Anthropic, Google Gemini, Kong, Svix,
Supabase, Vanta, and Drata have all adopted it. Anyone integrating
with Strathon already has off-the-shelf verifier libraries in seven
languages — they don't need to learn a Strathon-specific scheme.

Why hash the secret rather than store plaintext
-----------------------------------------------

If our database is compromised, an attacker should not be able to forge
webhooks to consumers. SHA-256 is one-way; an attacker who reads the
table cannot sign on behalf of the project. The cost is that we can't
display the plaintext after creation. Operators get the secret exactly
once via the POST response — same trade-off Stripe makes.

Rotation
--------

A project can have multiple non-revoked rows in ``webhook_signing_keys``.
When we send a webhook, ``compute_signature_headers`` includes one v1
signature per active key, space-delimited in the ``webhook-signature``
header. The consumer's verifier accepts any matching signature, so the
operator can roll keys without coordinating downtime:

  1. POST /v1/webhook_signing_keys                  -> new key A2
  2. Confirm consumers accept A2 (deliveries now sign with A1 + A2)
  3. DELETE /v1/webhook_signing_keys/{A1.id}        -> revoke old key
  4. Deliveries now sign with A2 only

This matches the multi-signature rotation pattern described in the
Standard Webhooks spec.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import datetime, timezone
from typing import Iterable

from standardwebhooks.webhooks import Webhook as _StdWebhook

logger = logging.getLogger("strathon.receiver.webhooks.signing")


# 4 chars chosen to give 16^4 = 65536 distinguishable prefixes per project.
# Far more than any operator will create by hand, and the prefix is not
# meant to be cryptographically distinguishing — just a human-readable
# handle for picking a key in operator UIs.
SIGNING_KEY_PREFIX_LEN = 4

# 24 bytes -> 32 base64 characters of entropy. Matches the recommendation
# in the Standard Webhooks spec and what Svix, Socket.dev, and others use
# for their whsec_* secrets.
_SECRET_RAW_BYTES = 24


def _make_plaintext_secret() -> str:
    """Generate a fresh whsec_-prefixed signing secret.

    Returns the plaintext form ready to be returned in the API response
    that creates the key. The caller is responsible for ensuring this
    value is shown to the operator exactly once and never logged or
    persisted by Strathon itself.
    """
    raw = secrets.token_bytes(_SECRET_RAW_BYTES)
    return "whsec_" + base64.b64encode(raw).decode("ascii")


def _make_prefix() -> str:
    """A short human-readable identifier for an existing signing key.

    Returns 4 lowercase hex characters from a fresh random byte. Two
    bytes of entropy is enough; the prefix is for picking, not for
    security.
    """
    return secrets.token_hex(2)  # 2 bytes -> 4 hex chars


def hash_secret(plaintext: str) -> bytes:
    """SHA-256 of the plaintext secret. The only form we persist.

    The plaintext starts with the ``whsec_`` literal prefix; we hash the
    full string including the prefix. This means a hash collision would
    require an attacker to produce a colliding ``whsec_...`` payload —
    not just any byte string.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).digest()


def create_signing_key() -> tuple[str, str, bytes]:
    """Generate a new signing key.

    Returns (plaintext, prefix, secret_hash). Caller persists prefix +
    secret_hash and returns plaintext to the operator exactly once. The
    plaintext is never written back to Strathon storage.
    """
    plaintext = _make_plaintext_secret()
    prefix = _make_prefix()
    return plaintext, prefix, hash_secret(plaintext)


def compute_signature_headers(
    secrets_plaintext: Iterable[str],
    webhook_id: str,
    body: str,
    now: datetime | None = None,
) -> dict[str, str]:
    """Build the three Standard Webhooks headers for a single delivery.

    Parameters
    ----------
    secrets_plaintext
        Iterable of active signing-secret plaintexts. Normally a project
        has exactly one; during rotation it has two; we tolerate any
        positive count and emit one ``v1,<sig>`` token per secret in the
        webhook-signature header, space-delimited. An empty iterable
        produces an empty signature, which consumers should reject — but
        we don't enforce that here because operators may legitimately
        choose to send unsigned webhooks to internal endpoints. In that
        case the project simply has zero non-revoked keys; we emit the
        id and timestamp headers but no signature header.

    webhook_id
        The stable Standard Webhooks msg id. Must be the same string on
        every retry of a given delivery so the consumer can dedupe.

    body
        The exact bytes of the request body that will be sent. The
        signature is computed over the body verbatim; if the caller
        re-serializes the JSON between signing and sending, the
        signature will not verify on the consumer side. This is the
        single most common Standard Webhooks bug; we sign-then-send the
        same string.

    now
        Override for the timestamp. Tests use this. In production it
        defaults to wall-clock UTC at call time.

    Returns
    -------
    dict[str, str]
        The headers to add to the outbound HTTP POST. Always contains
        ``webhook-id`` and ``webhook-timestamp``; contains
        ``webhook-signature`` if and only if at least one signing
        secret was provided.
    """
    ts = now or datetime.now(timezone.utc)
    headers = {
        "webhook-id": webhook_id,
        "webhook-timestamp": str(int(ts.timestamp())),
    }

    sig_tokens: list[str] = []
    for secret in secrets_plaintext:
        if not secret:
            continue
        # standardwebhooks.Webhook expects the whsec_ form and handles
        # base64 decoding internally. We don't strip the prefix.
        wh = _StdWebhook(secret)
        sig_tokens.append(wh.sign(webhook_id, ts, body))

    if sig_tokens:
        # Per the spec, multiple signatures are space-delimited inside
        # the single webhook-signature header. Each token already
        # carries its own scheme prefix (e.g., v1,<base64>).
        headers["webhook-signature"] = " ".join(sig_tokens)

    return headers


__all__ = [
    "SIGNING_KEY_PREFIX_LEN",
    "compute_signature_headers",
    "create_signing_key",
    "hash_secret",
]

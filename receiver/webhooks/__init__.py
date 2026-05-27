"""Reliable webhook delivery for Strathon alert policies.

This package is the receiver-side machinery that turns a matched `alert`
policy into an actual HTTP POST to an operator's webhook URL — with the
durability, retry, signing, and observability properties that make it
trustworthy for security-relevant alerts.

Layers
======

* ``signing``  - Standard Webhooks v1 HMAC-SHA256 over `{id}.{ts}.{body}`,
                 plus signing-key creation, hashing, prefix generation,
                 and rotation-aware multi-signature emission.

* ``broker``   - Dramatiq + Redis setup. Module-level broker initialized
                 lazily so importing this package does not connect to
                 Redis at module load (matters for tests and for booting
                 the receiver in environments where Redis is briefly
                 unavailable). Stub broker is used when STRATHON_WEBHOOK_QUEUE
                 is unset, so tests and dev environments without Redis
                 still function (signing/durability still work; sends just
                 happen inline in the dispatcher actor).

* ``actor``    - The Dramatiq actor that performs a single delivery
                 attempt. Built-in Retries middleware handles exponential
                 backoff with jitter; the actor itself classifies the
                 response and decides which exception to raise (or none,
                 for abandoned states) to drive that retry behavior.

* ``dispatch`` - enqueue_delivery(): the one function the receiver's
                 ingest path calls. Inserts the durable row and sends the
                 message to Dramatiq. Atomic at the DB layer; if the
                 Dramatiq send fails the row is still pending and the
                 sweeper will reclaim it.

Layering rule: callers outside this package import from this __init__,
not from submodules. The submodules can move; the public surface stays.
"""

from .dispatch import enqueue_delivery
from .signing import (
    create_signing_key,
    compute_signature_headers,
    hash_secret,
    SIGNING_KEY_PREFIX_LEN,
)

__all__ = [
    "enqueue_delivery",
    "create_signing_key",
    "compute_signature_headers",
    "hash_secret",
    "SIGNING_KEY_PREFIX_LEN",
]

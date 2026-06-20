"""HMAC-SHA256 hash chain compute over audit.events rows.

Each row's ``row_hash`` is computed as::

    row_hash = HMAC-SHA256(
        key = K_n,
        msg = canonical_json(row_payload) || prev_hash
    )

where ``row_payload`` is the subset of row fields that contribute to
identity (everything except ``prev_hash``, ``row_hash``, and
``hmac_key_id`` itself), and ``prev_hash`` is the ``row_hash`` of the
immediately preceding event for the same project.

This module is pure logic: it takes a row dict and a previous hash,
returns the new hash. The caller (the audit repository) is
responsible for fetching ``prev_hash`` under a per-project advisory
lock and for storing the result.

Verification reverses the operation: recompute the HMAC for each
row in sequence, compare against the stored value, expect equality.
A single mismatch indicates tampering somewhere between that row
and the previous anchor.

Tests live in ``tests/test_audit_hash_chain.py``.
"""

from __future__ import annotations

import hmac
from hashlib import sha256
from typing import Any

from .canonical import canonicalize


# Stable seed for the very first event ever recorded in a project's
# chain. The choice of value is conventional, not security-critical
# — what matters is that every project's first event has the same
# documented prev_hash so verification has a known starting point.
GENESIS_PREV_HASH: bytes = b"\x00" * 32


# Subset of AuditEvent column names that contribute to row_hash.
# Anything modifiable at insert time but NOT part of identity
# (ingested_at — clock-driven server timestamp) is excluded.
# Anything that would create a chicken-and-egg loop (prev_hash,
# row_hash, hmac_key_id) is excluded.
HASH_FIELDS: tuple[str, ...] = (
    "id",
    "sequence_no",
    "occurred_at",
    "project_id",
    "actor_type",
    "actor_id",
    "actor_display",
    "on_behalf_of",
    "action",
    "action_category",
    "outcome",
    "reason",
    "resource_type",
    "resource_id",
    "resource_parent",
    "cascade_root_id",
    "request_id",
    "source_ip",
    "user_agent",
    "api_key_id",
    "auth_method",
    "before_state",
    "after_state",
    "diff",
    "pii_classes",
    "schema_version",
)


def compute_row_hash(
    row: dict[str, Any],
    prev_hash: bytes,
    key: bytes,
) -> bytes:
    """Compute the HMAC-SHA256 row hash for an audit event.

    ``row`` must include every column listed in :data:`HASH_FIELDS`;
    extra keys are ignored. ``prev_hash`` is the immediately
    preceding row's ``row_hash`` (or :data:`GENESIS_PREV_HASH` for
    the first row in a project's chain). ``key`` is the project's
    HMAC key — 32+ bytes of high-entropy material.

    Raises ``KeyError`` if a required field is missing from ``row``;
    raises ``TypeError`` if a field's value isn't canonicalizable.
    """
    if len(key) < 32:
        raise ValueError(
            f"HMAC key too short: {len(key)} bytes; need >= 32"
        )
    if len(prev_hash) != 32:
        raise ValueError(
            f"prev_hash must be 32 bytes, got {len(prev_hash)}"
        )
    missing = [f for f in HASH_FIELDS if f not in row]
    if missing:
        raise KeyError(
            f"audit row missing required hash fields: {missing}"
        )
    payload = {f: row[f] for f in HASH_FIELDS}
    canonical = canonicalize(payload)
    mac = hmac.new(key, canonical + prev_hash, sha256)
    return mac.digest()


def verify_row(
    row: dict[str, Any],
    prev_hash: bytes,
    key: bytes,
    expected_row_hash: bytes,
) -> bool:
    """Constant-time check that ``row`` hashes to ``expected_row_hash``."""
    actual = compute_row_hash(row, prev_hash, key)
    return hmac.compare_digest(actual, expected_row_hash)


def merkle_root(hashes: list[bytes]) -> bytes:
    """Compute the Merkle Tree Hash over an ordered list of leaf inputs.

    Implements the RFC 6962 / RFC 9162 (Certificate Transparency) Merkle
    Tree Hash with **domain separation** and **no node duplication**:

    - ``MTH({}) = SHA-256()`` (hash of the empty string).
    - leaf:    ``SHA-256(0x00 || data)``
    - node:    ``SHA-256(0x01 || MTH(left) || MTH(right))`` where the list is
      split at ``k`` = the largest power of two strictly less than ``n``.

    Two properties this guarantees, both of which the previous
    duplicate-last-node construction lacked:

    1. **No CVE-2012-2459 collision.** Because odd levels are never padded by
       duplicating the final node, distinct leaf sequences cannot collapse to
       the same root (e.g. ``[1,2,3]`` and ``[1,2,3,3]`` differ).
    2. **Second-preimage resistance via domain separation.** Leaves and
       internal nodes are hashed with distinct ``0x00`` / ``0x01`` prefixes, so
       a 64-byte leaf cannot masquerade as the concatenation of two child
       digests (RFC 6962 S2.1: this domain separation is required for second
       preimage resistance). The HMAC-keyed leaf inputs do not remove this need
       — the attack is on tree structure, not leaf authenticity.

    The tree shape is uniquely determined by the number of leaves; leaves need
    not be a power of two and are never duplicated.
    """
    n = len(hashes)
    if n == 0:
        return sha256(b"").digest()
    if n == 1:
        return sha256(b"\x00" + hashes[0]).digest()
    # Largest power of two strictly less than n (RFC 6962 split point).
    k = 1
    while k << 1 < n:
        k <<= 1
    left = merkle_root(hashes[:k])
    right = merkle_root(hashes[k:])
    return sha256(b"\x01" + left + right).digest()

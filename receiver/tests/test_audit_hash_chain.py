"""Tests for audit/hash_chain.py."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime, timezone

import pytest

from audit.canonical import canonicalize
from audit.hash_chain import (
    GENESIS_PREV_HASH,
    HASH_FIELDS,
    compute_row_hash,
    merkle_root,
    verify_row,
)


def _build_row(**overrides):
    """Construct a complete row dict for hash compute."""
    base = {f: None for f in HASH_FIELDS}
    base.update({
        "id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "sequence_no": 1,
        "occurred_at": datetime(2026, 5, 17, 0, 0, 0, tzinfo=timezone.utc),
        "project_id": uuid.UUID("00000000-0000-0000-0000-000000000002"),
        "actor_type": "system",
        "actor_id": "test",
        "action": "policy.create",
        "action_category": "policy",
        "outcome": "allow",
        "resource_type": "policy",
        "resource_id": "pol_1",
        "request_id": uuid.UUID("00000000-0000-0000-0000-000000000003"),
        "pii_classes": [],
        "schema_version": 1,
    })
    base.update(overrides)
    return base


def test_compute_returns_32_bytes():
    key = b"x" * 32
    row = _build_row()
    h = compute_row_hash(row, GENESIS_PREV_HASH, key)
    assert isinstance(h, bytes)
    assert len(h) == 32


def test_short_key_rejected():
    row = _build_row()
    with pytest.raises(ValueError, match="HMAC key too short"):
        compute_row_hash(row, GENESIS_PREV_HASH, b"x" * 16)


def test_short_prev_hash_rejected():
    row = _build_row()
    with pytest.raises(ValueError, match="prev_hash must be 32 bytes"):
        compute_row_hash(row, b"\x00" * 16, b"x" * 32)


def test_missing_field_raises_keyerror():
    row = _build_row()
    del row["actor_type"]
    with pytest.raises(KeyError, match="actor_type"):
        compute_row_hash(row, GENESIS_PREV_HASH, b"x" * 32)


def test_determinism_same_row_same_key():
    key = b"x" * 32
    row = _build_row()
    a = compute_row_hash(row, GENESIS_PREV_HASH, key)
    b = compute_row_hash(dict(row), GENESIS_PREV_HASH, key)
    assert a == b


def test_different_keys_produce_different_hashes():
    row = _build_row()
    h1 = compute_row_hash(row, GENESIS_PREV_HASH, b"a" * 32)
    h2 = compute_row_hash(row, GENESIS_PREV_HASH, b"b" * 32)
    assert h1 != h2


def test_different_prev_hash_produces_different_hash():
    row = _build_row()
    key = b"x" * 32
    h1 = compute_row_hash(row, GENESIS_PREV_HASH, key)
    h2 = compute_row_hash(row, b"\xff" * 32, key)
    assert h1 != h2


def test_one_field_change_changes_hash():
    """Avalanche: any single field flip flips many bits in the hash."""
    key = b"x" * 32
    a = compute_row_hash(_build_row(actor_id="alice"), GENESIS_PREV_HASH, key)
    b = compute_row_hash(_build_row(actor_id="bob"), GENESIS_PREV_HASH, key)
    assert a != b
    # Expect at least 64 of 256 bits to differ (avalanche).
    diff_bits = sum(bin(x ^ y).count("1") for x, y in zip(a, b))
    assert diff_bits > 64


def test_verify_succeeds_for_correct_hash():
    key = b"x" * 32
    row = _build_row()
    h = compute_row_hash(row, GENESIS_PREV_HASH, key)
    assert verify_row(row, GENESIS_PREV_HASH, key, h) is True


def test_verify_fails_for_tampered_hash():
    key = b"x" * 32
    row = _build_row()
    h = compute_row_hash(row, GENESIS_PREV_HASH, key)
    tampered = bytes((h[0] ^ 1,)) + h[1:]
    assert verify_row(row, GENESIS_PREV_HASH, key, tampered) is False


def test_verify_fails_for_tampered_row():
    key = b"x" * 32
    row = _build_row()
    h = compute_row_hash(row, GENESIS_PREV_HASH, key)
    row["actor_id"] = "different"
    assert verify_row(row, GENESIS_PREV_HASH, key, h) is False


def test_compute_matches_explicit_hmac():
    """Cross-check the formula against a hand-written HMAC."""
    key = b"x" * 32
    row = _build_row()
    h = compute_row_hash(row, GENESIS_PREV_HASH, key)
    payload = {f: row[f] for f in HASH_FIELDS}
    expected = hmac.new(
        key,
        canonicalize(payload) + GENESIS_PREV_HASH,
        hashlib.sha256,
    ).digest()
    assert h == expected


def test_genesis_prev_hash_is_zeros():
    assert GENESIS_PREV_HASH == b"\x00" * 32


def test_chain_two_rows():
    """The second row's hash depends on the first row's hash."""
    key = b"x" * 32
    r1 = _build_row(sequence_no=1)
    r2 = _build_row(sequence_no=2, action="policy.update")
    h1 = compute_row_hash(r1, GENESIS_PREV_HASH, key)
    h2 = compute_row_hash(r2, h1, key)
    # If r1 had hashed differently, h2 differs too.
    r1_tampered = _build_row(sequence_no=1, actor_id="evil")
    h1_tampered = compute_row_hash(r1_tampered, GENESIS_PREV_HASH, key)
    h2_alt = compute_row_hash(r2, h1_tampered, key)
    assert h2 != h2_alt


def test_merkle_root_empty():
    # RFC 6962: MTH({}) = SHA-256() of the empty string.
    assert merkle_root([]) == hashlib.sha256(b"").digest()


def test_merkle_root_single():
    # RFC 6962 leaf hash: SHA-256(0x00 || data). NOT the bare leaf.
    h = b"a" * 32
    assert merkle_root([h]) == hashlib.sha256(b"\x00" + h).digest()


def test_merkle_root_two_leaves():
    """RFC 6962: node = SHA-256(0x01 || MTH(left) || MTH(right))."""
    left_in = b"a" * 32
    right_in = b"b" * 32
    left = hashlib.sha256(b"\x00" + left_in).digest()
    right = hashlib.sha256(b"\x00" + right_in).digest()
    expected = hashlib.sha256(b"\x01" + left + right).digest()
    assert merkle_root([left_in, right_in]) == expected


def test_merkle_root_odd_count_no_duplication():
    """RFC 6962: odd leaf counts split at the largest power of two below n;
    the final leaf is NEVER duplicated (the CVE-2012-2459 mistake)."""
    h1, h2, h3 = b"a" * 32, b"b" * 32, b"c" * 32
    leaf = lambda d: hashlib.sha256(b"\x00" + d).digest()  # noqa: E731
    node = lambda a, b: hashlib.sha256(b"\x01" + a + b).digest()  # noqa: E731
    # n=3, k=2: left = MTH([h1,h2]), right = MTH([h3])
    left = node(leaf(h1), leaf(h2))
    right = leaf(h3)
    expected = node(left, right)
    assert merkle_root([h1, h2, h3]) == expected


def test_merkle_root_cve_2012_2459_no_collision():
    """Regression: distinct leaf sets must not collapse to the same root.

    Under the old duplicate-last-node construction, [1,2,3] and [1,2,3,3]
    produced identical roots (CVE-2012-2459). The RFC 6962 construction must
    keep them distinct.
    """
    a = [b"1" * 32, b"2" * 32, b"3" * 32]
    b = [b"1" * 32, b"2" * 32, b"3" * 32, b"3" * 32]
    assert merkle_root(a) != merkle_root(b)
    # And a six-leaf variant of the classic example.
    six = [bytes([i]) * 32 for i in range(1, 7)]
    padded = six + [six[-2], six[-1]]  # [..,5,6,5,6]
    assert merkle_root(six) != merkle_root(padded)


def test_merkle_root_leaf_node_domain_separation():
    """A 64-byte leaf input must not collide with an internal node over two
    32-byte children (second-preimage / leaf-node confusion). Domain
    separation (0x00 vs 0x01 prefix) guarantees this."""
    c1, c2 = b"a" * 32, b"b" * 32
    # Internal node over two single-leaf subtrees.
    node_root = merkle_root([c1, c2])
    # A single leaf whose data is the 64-byte concatenation of the two child
    # inputs. With domain separation this hashes under 0x00, not 0x01, so it
    # cannot equal the internal node.
    confused_leaf = merkle_root([c1 + c2])
    assert node_root != confused_leaf


def test_merkle_root_change_detects_any_leaf():
    leaves = [b"\x00" * 32 for _ in range(8)]
    root_a = merkle_root(leaves)
    leaves[3] = b"\x01" * 32
    root_b = merkle_root(leaves)
    assert root_a != root_b

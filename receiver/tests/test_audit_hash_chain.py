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
    assert merkle_root([]) == b"\x00" * 32


def test_merkle_root_single():
    h = b"a" * 32
    assert merkle_root([h]) == h


def test_merkle_root_two_leaves():
    """Combine two leaves via SHA-256(left || right)."""
    left = b"a" * 32
    right = b"b" * 32
    expected = hashlib.sha256(left + right).digest()
    assert merkle_root([left, right]) == expected


def test_merkle_root_odd_count_duplicates_last():
    """RFC 6962 style: odd leaves pad by duplicating the last."""
    h1, h2, h3 = b"a" * 32, b"b" * 32, b"c" * 32
    # Level 0: [h1, h2, h3] -> [h1, h2, h3, h3]
    # Level 1: [SHA(h1||h2), SHA(h3||h3)]
    # Root: SHA(level1_left || level1_right)
    left = hashlib.sha256(h1 + h2).digest()
    right = hashlib.sha256(h3 + h3).digest()
    expected = hashlib.sha256(left + right).digest()
    assert merkle_root([h1, h2, h3]) == expected


def test_merkle_root_change_detects_any_leaf():
    leaves = [b"\x00" * 32 for _ in range(8)]
    root_a = merkle_root(leaves)
    leaves[3] = b"\x01" * 32
    root_b = merkle_root(leaves)
    assert root_a != root_b

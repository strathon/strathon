"""Unit tests for the receiver's auth module.

Pure helpers only — DB-touching code is exercised end-to-end through the
demos and the existing curl-based integration test runs.
"""

import sys

# Receiver is not packaged; add it to sys.path so we can import directly.
sys.path.insert(0, "/home/claude/strathon/receiver")

import pytest

from auth import (
    KEY_PREFIX_LEN,
    KEY_SCHEME,
    _extract_bearer_token,
    _sha256_hex,
    generate_api_key,
)


# ---- generate_api_key ----


def test_generated_key_has_strathon_prefix():
    raw, prefix, key_hash = generate_api_key()
    assert raw.startswith(KEY_SCHEME)


def test_generated_key_prefix_matches_first_n_chars():
    raw, prefix, key_hash = generate_api_key()
    assert prefix == raw[:KEY_PREFIX_LEN]
    assert len(prefix) == KEY_PREFIX_LEN


def test_generated_key_hash_is_sha256_hex():
    raw, prefix, key_hash = generate_api_key()
    # SHA-256 hex is 64 characters
    assert len(key_hash) == 64
    # Hex only contains 0-9a-f
    assert all(c in "0123456789abcdef" for c in key_hash)
    # Verify the hash actually matches the key
    assert key_hash == _sha256_hex(raw)


def test_generated_keys_are_unique():
    """Two consecutive generations must produce different keys (256 bits of entropy)."""
    keys = {generate_api_key()[0] for _ in range(50)}
    assert len(keys) == 50


def test_generated_key_has_enough_entropy():
    """The random part should be at least ~32 base64url chars (256 bits entropy)."""
    raw, _, _ = generate_api_key()
    random_part = raw[len(KEY_SCHEME):]
    # base64url(32 bytes) -> 43 chars
    assert len(random_part) >= 40


# ---- _extract_bearer_token ----


def test_extract_bearer_token_strips_prefix():
    assert _extract_bearer_token("Bearer foo") == "foo"


def test_extract_bearer_token_is_case_insensitive_on_scheme():
    assert _extract_bearer_token("bearer foo") == "foo"
    assert _extract_bearer_token("BEARER foo") == "foo"
    assert _extract_bearer_token("BeArEr foo") == "foo"


def test_extract_bearer_token_returns_none_on_missing():
    assert _extract_bearer_token(None) is None
    assert _extract_bearer_token("") is None


def test_extract_bearer_token_returns_none_on_malformed():
    assert _extract_bearer_token("Basic abc") is None  # wrong scheme
    assert _extract_bearer_token("Bearer") is None  # missing token
    assert _extract_bearer_token("Bearer  ") is None  # empty token after spaces


def test_extract_bearer_token_handles_spaces_around_token():
    # Whitespace after the scheme is the separator; trailing whitespace stripped
    assert _extract_bearer_token("Bearer   token_value  ") == "token_value"


# ---- _sha256_hex ----


def test_sha256_hex_is_deterministic():
    h1 = _sha256_hex("foo")
    h2 = _sha256_hex("foo")
    assert h1 == h2


def test_sha256_hex_matches_known_value():
    """Verify our seeded dev key hash matches the migration."""
    raw = "stra_dev_local_default_project_do_not_use_in_production"
    assert _sha256_hex(raw) == (
        "d167e0111ebddd7e1001ad51ded8b7f9f7887c127a626063a83e02b6e6807924"
    )


def test_sha256_hex_returns_64_chars():
    assert len(_sha256_hex("anything")) == 64

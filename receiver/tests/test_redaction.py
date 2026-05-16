"""Tests for receiver/redaction.py.

We organize tests by the property each verifies:

  - Detection: every default entity is found in canonical examples
  - Validators: Luhn rejects false-positive credit-card numbers
  - Actions: redact / mask / hash / delete produce expected outputs
  - Attribute-level handling: key actions + allowlist + non-string values
  - Disabled config is a no-op
  - Custom patterns run after defaults
  - The non-mutation guarantee: redact_attributes returns a new dict
"""

from __future__ import annotations

import hashlib
import os
import re
import sys

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)

from redaction import (  # noqa: E402
    RedactionConfig,
    VALID_KEY_ACTIONS,
    VALID_VALUE_ACTIONS,
    _luhn_check,
    redact_attributes,
    redact_string,
    validate_key_actions,
    validate_strategy,
)


# ---- Detection: each default entity is caught ---------------------------


def test_email_detected_and_redacted_by_default():
    out = redact_string("contact alice@example.com please")
    assert out == "contact [EMAIL_ADDRESS] please"


def test_credit_card_with_valid_luhn_redacted():
    # 4242 4242 4242 4242 is the canonical Stripe test card; valid Luhn.
    out = redact_string("card 4242 4242 4242 4242 expires soon")
    assert "[CREDIT_CARD]" in out
    assert "4242" not in out


def test_credit_card_with_invalid_luhn_left_alone():
    """0123456789012345 satisfies the 13-19-digit regex but fails Luhn —
    a generic order number, account ID, etc. Must NOT be redacted."""
    out = redact_string("order 0123456789012345 confirmed")
    assert "0123456789012345" in out
    assert "[CREDIT_CARD]" not in out


def test_us_ssn_detected():
    out = redact_string("ssn 123-45-6789 on file")
    assert out == "ssn [US_SSN] on file"


def test_us_ssn_no_hyphen_not_falsely_matched():
    """9 consecutive digits without hyphens is too noisy a pattern
    (account numbers, order IDs) — we deliberately don't match it."""
    out = redact_string("account 123456789 active")
    assert "123456789" in out  # left alone


def test_phone_us_detected():
    cases = ["(555) 123-4567", "555-123-4567", "555.123.4567"]
    for c in cases:
        out = redact_string(f"call {c} today")
        assert "[PHONE_NUMBER]" in out, f"failed for {c!r}: got {out!r}"


def test_phone_us_does_not_match_inside_long_digit_runs():
    """A phone-shaped substring inside a longer digit sequence should
    not be redacted. The lookbehind / lookahead guards prevent it."""
    out = redact_string("ref 9999555.123.45670000 ok")
    # The phone-shaped portion is inside a longer digit run, so the
    # negative lookahead `(?!\d)` blocks the match.
    assert "[PHONE_NUMBER]" not in out


def test_ipv4_detected():
    out = redact_string("from 192.168.1.42 today")
    assert "[IP_ADDRESS]" in out


def test_openai_api_key_detected():
    """sk-... is the highest-impact pattern. A leaked OpenAI key in a
    trace store is a real-world security incident."""
    out = redact_string("export OPENAI_API_KEY=sk-1234567890abcdefghijklmnop")
    assert "[API_KEY]" in out
    assert "sk-1234567890" not in out


def test_stripe_secret_key_detected():
    out = redact_string("STRIPE_SECRET=sk_live_abc123def456ghi789jkl0")
    assert "[API_KEY]" in out


def test_github_pat_detected():
    out = redact_string("token ghp_abcdefghijklmnopqrstuvwxyz1234567890")
    assert "[API_KEY]" in out


def test_jwt_detected():
    """JWTs in logs are a privacy issue (user identity) and a security
    issue (session hijacking if not expired). Catch them."""
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyMTIzIn0.signature_part_here"
    out = redact_string(f"auth header Bearer {jwt}")
    assert "[API_KEY]" in out
    assert "eyJhbGciOi" not in out


# ---- Luhn validator unit tests -----------------------------------------


def test_luhn_accepts_known_valid_test_cards():
    valid = [
        "4242424242424242",  # Visa test
        "5555555555554444",  # Mastercard test
        "378282246310005",   # Amex test
    ]
    for n in valid:
        assert _luhn_check(n) is True, f"Luhn failed for known-valid {n}"


def test_luhn_rejects_obviously_wrong():
    assert _luhn_check("1234567890123456") is False
    assert _luhn_check("4242424242424243") is False  # off-by-one fails


def test_luhn_rejects_too_short_or_too_long():
    assert _luhn_check("123") is False
    assert _luhn_check("1" * 20) is False


# ---- Actions: redact, mask, hash ---------------------------------------


def test_mask_keeps_last_four_chars():
    out = redact_string(
        "card 4242424242424242 today",
        strategy={"CREDIT_CARD": "mask"},
    )
    # Mask preserves last 4 of the matched digits.
    assert "4242" in out  # last 4 visible
    # Most of the digits are replaced with *
    assert "*" * 12 in out


def test_hash_action_produces_deterministic_output():
    """Two runs against the same input must produce the same hash,
    so analytics that join on the hashed value still work."""
    out1 = redact_string(
        "alice@example.com",
        strategy={"EMAIL_ADDRESS": "hash"},
    )
    out2 = redact_string(
        "alice@example.com",
        strategy={"EMAIL_ADDRESS": "hash"},
    )
    assert out1 == out2
    # Hash output format: [ENTITY:12_HEX_CHARS]
    assert "[EMAIL_ADDRESS:" in out1
    assert out1.endswith("]")
    # Verify the hash prefix actually matches SHA-256 of the input
    expected = hashlib.sha256("alice@example.com".encode("utf-8")).hexdigest()[:12]
    assert expected in out1


def test_different_inputs_hash_to_different_outputs():
    out1 = redact_string("alice@example.com", strategy={"EMAIL_ADDRESS": "hash"})
    out2 = redact_string("bob@example.com",   strategy={"EMAIL_ADDRESS": "hash"})
    assert out1 != out2


# ---- Multi-entity in one string ----------------------------------------


def test_multiple_entities_in_one_string():
    text = "email alice@example.com, phone 555-123-4567, ssn 123-45-6789"
    out = redact_string(text)
    assert "[EMAIL_ADDRESS]" in out
    assert "[PHONE_NUMBER]" in out
    assert "[US_SSN]" in out
    # Original PII fully gone
    assert "alice@example.com" not in out
    assert "555-123-4567" not in out
    assert "123-45-6789" not in out


def test_per_entity_actions_can_differ():
    out = redact_string(
        "email alice@example.com, ssn 123-45-6789",
        strategy={"EMAIL_ADDRESS": "hash", "US_SSN": "redact"},
    )
    assert "[EMAIL_ADDRESS:" in out  # hashed
    assert "[US_SSN]" in out          # redacted plain


# ---- Edge cases on input ----------------------------------------------


def test_empty_string():
    assert redact_string("") == ""


def test_none_value_returned_unchanged():
    # Type-wise we expect strings, but defensive: a None must pass through
    assert redact_string(None) is None  # type: ignore[arg-type]


def test_string_with_no_pii_unchanged():
    text = "just a normal sentence with nothing sensitive"
    assert redact_string(text) == text


def test_email_pattern_does_not_overmatch():
    """foo@bar (no TLD with 2+ chars) should NOT be flagged."""
    out = redact_string("foo@bar is not an email")
    assert "[EMAIL_ADDRESS]" not in out


# ---- redact_attributes: key actions, allowlist, mixed types -----------


def test_disabled_config_is_passthrough():
    attrs = {"strathon.tool.args": "send to alice@example.com"}
    out = redact_attributes(attrs, RedactionConfig.disabled())
    # Same content, unredacted
    assert out["strathon.tool.args"] == "send to alice@example.com"


def test_value_scan_on_string_attribute():
    cfg = RedactionConfig(
        enabled=True, strategy={}, key_actions={},
        allowlist=(), custom_patterns=(),
    )
    attrs = {"strathon.tool.args": "email alice@example.com"}
    out = redact_attributes(attrs, cfg)
    assert "[EMAIL_ADDRESS]" in out["strathon.tool.args"]


def test_key_action_delete_drops_attribute():
    cfg = RedactionConfig(
        enabled=True, strategy={},
        key_actions={"http.request.header.authorization": "delete"},
        allowlist=(), custom_patterns=(),
    )
    attrs = {
        "http.request.header.authorization": "Bearer sk-secrettoken",
        "http.method": "POST",
    }
    out = redact_attributes(attrs, cfg)
    assert "http.request.header.authorization" not in out
    assert out["http.method"] == "POST"


def test_key_action_hash_transforms_whole_value():
    cfg = RedactionConfig(
        enabled=True, strategy={},
        key_actions={"user.email": "hash"},
        allowlist=(), custom_patterns=(),
    )
    attrs = {"user.email": "alice@example.com"}
    out = redact_attributes(attrs, cfg)
    assert out["user.email"].startswith("[USER_EMAIL:")
    assert "alice@example.com" not in out["user.email"]


def test_allowlist_drops_everything_not_listed():
    cfg = RedactionConfig(
        enabled=True, strategy={}, key_actions={},
        allowlist=("http.method", "service.name"),
        custom_patterns=(),
    )
    attrs = {
        "http.method": "POST",
        "service.name": "agent-svc",
        "user.email": "alice@example.com",
        "strathon.tool.args": "anything",
    }
    out = redact_attributes(attrs, cfg)
    assert set(out.keys()) == {"http.method", "service.name"}


def test_non_string_attributes_left_alone():
    cfg = RedactionConfig(
        enabled=True, strategy={}, key_actions={},
        allowlist=(), custom_patterns=(),
    )
    attrs = {
        "gen_ai.usage.input_tokens": 1234,
        "is_streaming": True,
        "latency_ms": 12.5,
    }
    out = redact_attributes(attrs, cfg)
    assert out == attrs


def test_redact_attributes_does_not_mutate_input():
    """The caller needs the original (unredacted) attrs for policy
    evaluation. We must return a new dict; the input must be unchanged."""
    cfg = RedactionConfig(
        enabled=True, strategy={}, key_actions={},
        allowlist=(), custom_patterns=(),
    )
    original = {"strathon.tool.args": "email alice@example.com"}
    snapshot = dict(original)
    out = redact_attributes(original, cfg)
    assert original == snapshot  # unchanged
    assert out is not original  # new dict
    assert "[EMAIL_ADDRESS]" in out["strathon.tool.args"]


# ---- Custom patterns -------------------------------------------------


def test_custom_pattern_runs_after_defaults():
    """An operator can declare a custom regex (e.g. internal account
    IDs) that gets the same treatment as a default entity."""
    cfg = RedactionConfig(
        enabled=True,
        strategy={"INTERNAL_ID": "redact"},
        key_actions={},
        allowlist=(),
        custom_patterns=(("INTERNAL_ID", re.compile(r"\bACCT-\d{8}\b")),),
    )
    attrs = {"strathon.tool.args": "for ACCT-12345678"}
    out = redact_attributes(attrs, cfg)
    assert "[INTERNAL_ID]" in out["strathon.tool.args"]
    assert "ACCT-12345678" not in out["strathon.tool.args"]


# ---- Validators -----------------------------------------------------


def test_validate_strategy_accepts_known_actions():
    for action in VALID_VALUE_ACTIONS:
        validate_strategy({"EMAIL_ADDRESS": action})


def test_validate_strategy_rejects_unknown_action():
    with pytest.raises(ValueError, match="invalid action"):
        validate_strategy({"EMAIL_ADDRESS": "encrypt"})


def test_validate_key_actions_accepts_delete_for_keys():
    """delete is valid for whole-attribute actions (key actions) but
    not for value-pattern actions; the two validators differ on this."""
    assert "delete" in VALID_KEY_ACTIONS
    assert "delete" not in VALID_VALUE_ACTIONS
    validate_key_actions({"user.email": "delete"})  # must not raise


def test_validate_key_actions_rejects_unknown():
    with pytest.raises(ValueError, match="invalid action"):
        validate_key_actions({"user.email": "encrypt"})

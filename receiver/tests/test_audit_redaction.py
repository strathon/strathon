"""Tests for audit/redaction.py."""

from __future__ import annotations

from audit.redaction import hmac_value, redact_state


KEY = b"x" * 32


def test_passthrough_for_unmatched_keys():
    out = redact_state({"name": "foo", "priority": 50}, KEY)
    assert out == {"name": "foo", "priority": 50}


def test_excluded_keys_are_removed():
    out = redact_state({"name": "foo", "api_key": "sk_secret"}, KEY)
    assert "api_key" not in out
    assert out == {"name": "foo"}


def test_redacted_keys_get_placeholder():
    out = redact_state({"password": "hunter2", "name": "x"}, KEY)
    assert out["password"] == "[REDACTED]"
    assert out["name"] == "x"


def test_hmac_keys_get_hashed_form():
    out = redact_state(
        {"external_user_id": "user_123", "name": "x"}, KEY
    )
    assert out["external_user_id"].startswith("hmac-sha256:")
    assert "user_123" not in out["external_user_id"]
    assert out["name"] == "x"


def test_nested_dict_redacted_recursively():
    out = redact_state(
        {"meta": {"name": "x", "api_key": "k", "password": "p"}}, KEY
    )
    assert out == {"meta": {"name": "x", "password": "[REDACTED]"}}


def test_list_of_dicts_redacted_per_element():
    out = redact_state([{"name": "a", "secret": "s"}, {"name": "b"}], KEY)
    assert out == [{"name": "a"}, {"name": "b"}]


def test_none_passes_through():
    assert redact_state(None, KEY) is None


def test_input_not_mutated():
    original = {"name": "x", "secret": "y"}
    out = redact_state(original, KEY)
    assert original == {"name": "x", "secret": "y"}
    assert out == {"name": "x"}


def test_excluded_field_inside_nested_list():
    out = redact_state(
        {"items": [{"value": "v1"}, {"value": "v2"}, {"name": "ok"}]}, KEY
    )
    assert out == {"items": [{}, {}, {"name": "ok"}]}


def test_hmac_value_deterministic():
    a = hmac_value("user_123", KEY)
    b = hmac_value("user_123", KEY)
    assert a == b
    assert a.startswith("hmac-sha256:")


def test_hmac_value_different_for_different_inputs():
    a = hmac_value("user_123", KEY)
    b = hmac_value("user_124", KEY)
    assert a != b


def test_multiple_redaction_strategies_in_one_dict():
    out = redact_state(
        {
            "name": "ok",
            "password": "pw",
            "api_key": "ak",
            "external_user_id": "uid_1",
        },
        KEY,
    )
    assert "api_key" not in out
    assert out["name"] == "ok"
    assert out["password"] == "[REDACTED]"
    assert out["external_user_id"].startswith("hmac-sha256:")


def test_token_excluded():
    out = redact_state({"token": "t", "data": "x"}, KEY)
    assert out == {"data": "x"}


def test_signing_key_excluded():
    out = redact_state({"signing_key": "whsec_xxx", "id": "1"}, KEY)
    assert out == {"id": "1"}


def test_scalar_value_passes_through():
    assert redact_state("just-a-string", KEY) == "just-a-string"
    assert redact_state(42, KEY) == 42
    assert redact_state(True, KEY) is True

"""Tests for audit/scim_filter.py."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from audit.scim_filter import ParseError, compile_to_sql, parse


# --- Parser correctness -----------------------------------------------------


def test_simple_eq_compare():
    sql, params = compile_to_sql('action_category eq "policy"')
    assert sql == "action_category = %s"
    assert params == ["policy"]


def test_all_simple_operators():
    sql, params = compile_to_sql('outcome ne "allow"')
    assert sql == "outcome != %s"
    assert params == ["allow"]
    sql, params = compile_to_sql('actor_id sw "user_"')
    assert "LIKE" in sql
    assert params == ["user\\_%"]


def test_starts_with_operator():
    sql, params = compile_to_sql('actor_id sw "admin"')
    assert sql == "actor_id LIKE %s"
    assert params == ["admin%"]


def test_ends_with_operator():
    sql, params = compile_to_sql('actor_id ew "@example.com"')
    assert sql == "actor_id LIKE %s"
    assert params == ["%@example.com"]


def test_contains_operator():
    sql, params = compile_to_sql('reason co "denied"')
    assert sql == "reason LIKE %s"
    assert params == ["%denied%"]


def test_like_special_chars_escaped():
    sql, params = compile_to_sql('actor_id sw "50%_off"')
    assert "\\%" in params[0]
    assert "\\_" in params[0]


def test_logical_and():
    sql, params = compile_to_sql(
        'action_category eq "policy" and outcome eq "deny"'
    )
    assert sql == "(action_category = %s AND outcome = %s)"
    assert params == ["policy", "deny"]


def test_logical_or():
    sql, params = compile_to_sql(
        'outcome eq "deny" or outcome eq "error"'
    )
    assert "OR" in sql
    assert params == ["deny", "error"]


def test_logical_not():
    sql, params = compile_to_sql('not (outcome eq "allow")')
    assert sql.startswith("(NOT")
    assert params == ["allow"]


def test_parentheses_override_precedence():
    sql, _ = compile_to_sql(
        'action_category eq "policy" '
        'and (outcome eq "deny" or outcome eq "error")'
    )
    assert "(action_category = %s AND" in sql
    assert "(outcome = %s OR outcome = %s)" in sql


def test_and_binds_tighter_than_or():
    """a or b and c → a or (b and c)."""
    sql, _ = compile_to_sql(
        'action_category eq "a" or outcome eq "b" and reason eq "c"'
    )
    # Expect: (a OR (b AND c))
    assert sql == "(action_category = %s OR (outcome = %s AND reason = %s))"


def test_datetime_coerced_from_iso():
    sql, params = compile_to_sql('occurred_at ge "2026-01-01T00:00:00Z"')
    assert sql == "occurred_at >= %s"
    assert isinstance(params[0], datetime)
    assert params[0] == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_uuid_coerced():
    u = "00000000-0000-0000-0000-000000000001"
    sql, params = compile_to_sql(f'project_id eq "{u}"')
    assert isinstance(params[0], uuid.UUID)


# --- Error cases ------------------------------------------------------------


def test_empty_filter_raises():
    with pytest.raises(ParseError, match="empty"):
        parse("")


def test_unknown_attribute_raises():
    with pytest.raises(ParseError, match="not filterable"):
        compile_to_sql('unknown_field eq "x"')


def test_unknown_operator_raises():
    with pytest.raises(ParseError):
        parse('actor_id badop "x"')


def test_missing_value_raises():
    with pytest.raises(ParseError):
        parse('actor_id eq')


def test_unterminated_string_raises():
    with pytest.raises(ParseError):
        parse('actor_id eq "unterminated')


def test_bad_uuid_literal_raises():
    with pytest.raises(ParseError, match="uuid"):
        compile_to_sql('project_id eq "not-a-uuid"')


def test_bad_datetime_literal_raises():
    with pytest.raises(ParseError, match="datetime"):
        compile_to_sql('occurred_at eq "not-iso"')


def test_co_on_non_string_raises():
    # Numeric on a text-like operator
    with pytest.raises(ParseError):
        compile_to_sql('actor_id co 42')


def test_unbalanced_parens_raises():
    with pytest.raises(ParseError):
        parse('(actor_id eq "x"')


def test_trailing_garbage_raises():
    with pytest.raises(ParseError):
        parse('actor_id eq "x" garbage')


# --- Allowlist enforcement --------------------------------------------------


def test_filterable_columns_set():
    """Sanity-check the allowlist hasn't lost an expected column."""
    expected = {
        "occurred_at", "ingested_at", "project_id", "actor_type",
        "actor_id", "action", "action_category", "outcome", "reason",
        "resource_type", "resource_id", "request_id", "api_key_id",
        "auth_method",
    }
    for attr in expected:
        # Each should compile without error
        compile_to_sql(f'{attr} eq "x"' if attr not in {"occurred_at", "ingested_at", "project_id", "request_id"} else
                       f'{attr} eq "00000000-0000-0000-0000-000000000001"' if "id" in attr and attr != "actor_id" and attr != "api_key_id" else
                       f'{attr} ge "2026-01-01T00:00:00Z"')


def test_sequence_no_not_filterable():
    """sequence_no is intentionally not exposed to filters."""
    with pytest.raises(ParseError):
        compile_to_sql('sequence_no eq 1')


def test_hash_columns_not_filterable():
    """Hash columns are not filterable: they're opaque integrity data."""
    with pytest.raises(ParseError):
        compile_to_sql('row_hash eq "abc"')
    with pytest.raises(ParseError):
        compile_to_sql('prev_hash eq "abc"')


def test_state_columns_not_filterable():
    """Before/after state are JSONB; filtering would need GIN logic."""
    with pytest.raises(ParseError):
        compile_to_sql('after_state eq "x"')

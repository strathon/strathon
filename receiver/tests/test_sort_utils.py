"""Sort-parameter parsing must allowlist columns and fall back safely.

The offset query behavior itself is covered by the repository integration
tests; this pins the pure parsing/allowlist logic that decides whether a sort
is honored at all -- an unknown or malformed key must resolve to the default
order, never to an injected column.
"""

from __future__ import annotations

from sort_utils import parse_sort

ALLOW = {"duration": "duration_ms", "started": "start_time_unix_nano"}


def test_none_and_empty_fall_back_to_default():
    assert parse_sort(None, ALLOW) == (None, True)
    assert parse_sort("", ALLOW) == (None, True)


def test_known_field_desc_is_default_direction():
    assert parse_sort("duration", ALLOW) == ("duration_ms", True)
    assert parse_sort("duration:desc", ALLOW) == ("duration_ms", True)


def test_known_field_asc():
    assert parse_sort("duration:asc", ALLOW) == ("duration_ms", False)


def test_unknown_field_falls_back_not_injected():
    # A column name that is not an allowlist KEY must not leak through, even if
    # it happens to be a real column value.
    assert parse_sort("duration_ms:asc", ALLOW) == (None, True)
    assert parse_sort("id; DROP TABLE spans:desc", ALLOW) == (None, True)


def test_case_and_whitespace_insensitive_key_and_direction():
    assert parse_sort("  Duration : ASC ", ALLOW) == ("duration_ms", False)


def test_garbage_direction_defaults_to_desc():
    assert parse_sort("duration:sideways", ALLOW) == ("duration_ms", True)

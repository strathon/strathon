"""Tests for audit/canonical.py.

The canonicalizer's contract is "two semantically-equal inputs
produce byte-identical output." These tests enumerate the
type-by-type rules and verify the determinism property directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import pytest

from audit.canonical import canonicalize


def test_dict_keys_sorted_lexicographically():
    a = canonicalize({"b": 1, "a": 2, "c": 3})
    b = canonicalize({"c": 3, "a": 2, "b": 1})
    assert a == b
    assert a == b'{"a":2,"b":1,"c":3}'


def test_nested_dict_keys_sorted_at_each_level():
    out = canonicalize({"b": {"y": 1, "x": 2}, "a": 3})
    assert out == b'{"a":3,"b":{"x":2,"y":1}}'


def test_no_insignificant_whitespace():
    out = canonicalize({"a": [1, 2, 3]})
    assert b" " not in out
    assert b"\n" not in out


def test_unicode_strings_passed_through():
    out = canonicalize({"name": "héllo"})
    assert out == '{"name":"héllo"}'.encode("utf-8")


def test_datetime_must_be_aware():
    with pytest.raises(TypeError, match="naive datetime"):
        canonicalize({"t": datetime(2026, 1, 1)})


def test_datetime_normalized_to_utc():
    """Two equivalent moments in different zones canonicalize the same."""
    east = timezone(timedelta(hours=5, minutes=30))
    moment_utc = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    moment_east = datetime(2026, 5, 17, 17, 30, 0, tzinfo=east)
    assert canonicalize(moment_utc) == canonicalize(moment_east)


def test_datetime_microsecond_precision_preserved():
    moment = datetime(2026, 5, 17, 12, 0, 0, 123456, tzinfo=timezone.utc)
    out = canonicalize(moment)
    assert b"123456" in out


def test_uuid_lowercase_hex():
    u = uuid.UUID("DEADBEEF-1234-5678-1234-DEADBEEFFEED")
    out = canonicalize(u)
    assert out == b'"deadbeef-1234-5678-1234-deadbeeffeed"'


def test_bytes_emitted_as_hex_prefixed():
    out = canonicalize({"k": b"\xde\xad\xbe\xef"})
    assert out == b'{"k":"0xdeadbeef"}'


def test_set_sorted_as_list():
    out = canonicalize({"tags": {"c", "a", "b"}})
    assert out == b'{"tags":["a","b","c"]}'


def test_tuple_treated_as_list():
    out = canonicalize({"v": (1, 2, 3)})
    assert out == b'{"v":[1,2,3]}'


def test_nan_rejected():
    with pytest.raises(TypeError):
        canonicalize({"x": float("nan")})


def test_inf_rejected():
    with pytest.raises(TypeError):
        canonicalize({"x": float("inf")})


def test_unknown_type_rejected():
    class Custom:
        pass
    with pytest.raises(TypeError, match="not canonicalizable"):
        canonicalize({"x": Custom()})


def test_nested_uuid_and_bytes_canonicalized():
    """Realistic audit row shape."""
    u = uuid.UUID("00000000-0000-0000-0000-000000000001")
    row = {
        "id": u,
        "prev_hash": b"\x00" * 32,
        "actor": {"id": "test", "type": "system"},
    }
    out = canonicalize(row)
    # Stable output — locked golden value.
    assert (
        b'"id":"00000000-0000-0000-0000-000000000001"' in out
    )
    assert (
        b'"prev_hash":"0x0000000000000000000000000000000000000000000000000000000000000000"'
        in out
    )


def test_bool_emitted_lowercase():
    assert canonicalize({"x": True}) == b'{"x":true}'
    assert canonicalize({"x": False}) == b'{"x":false}'


def test_null_emitted_lowercase():
    assert canonicalize({"x": None}) == b'{"x":null}'


def test_int_emitted_bare():
    assert canonicalize({"n": 42}) == b'{"n":42}'


def test_empty_dict_and_list():
    assert canonicalize({}) == b'{}'
    assert canonicalize([]) == b'[]'


def test_determinism_across_constructions():
    """Build the same dict two different ways; output must match byte-for-byte."""
    a = {"a": 1, "b": [1, 2, 3]}
    b = {}
    b["b"] = [1, 2, 3]
    b["a"] = 1
    assert canonicalize(a) == canonicalize(b)

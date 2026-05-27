"""Canonical JSON serialization for hash-chain input.

The HMAC chain over ``audit.events`` rows requires a deterministic
byte representation of the row contents. Two different in-memory
shapes that hash to the same canonical string MUST produce the same
HMAC; equivalently, two semantically-identical events must verify
under one another's HMAC.

We use a strict subset of RFC 8785 JCS (JSON Canonicalization
Scheme) sufficient for our row shape:

- Object keys are sorted lexicographically.
- Output is UTF-8 with no insignificant whitespace.
- Strings are escaped per JSON.stringify rules.
- Integers up to 2^53 - 1 are emitted as bare integers.
- Floats are emitted with the shortest round-tripping decimal.
- Booleans and null are lowercase.
- Datetimes are ISO 8601 with UTC offset (``...+00:00``), microsecond
  precision, never truncated. Aware datetimes only — naive datetimes
  raise.
- UUIDs are emitted as lowercase hex strings ``xxxxxxxx-xxxx-...``.
- Bytes are emitted as lowercase hex strings prefixed with ``0x``.

This is intentionally more restrictive than full JSON, because the
input domain is well-known: row dictionaries built by
:func:`canonicalize_event_row`. The function rejects values it
cannot canonicalize rather than guessing.

Tests live in ``tests/test_audit_canonical.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any


def canonicalize(value: Any) -> bytes:
    """Return the canonical UTF-8 byte representation of ``value``.

    Raises ``TypeError`` if ``value`` contains a type the
    canonicalizer doesn't know how to render deterministically.
    """
    return _to_canonical_json(value).encode("utf-8")


def _to_canonical_json(value: Any) -> str:
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _normalize(value: Any) -> Any:
    """Recursively map a value into a JSON-serializable form.

    Anything not natively JSON gets converted to a string per the
    rules in this module's docstring. Mappings are returned as
    plain ``dict`` (json.dumps with ``sort_keys=True`` handles the
    ordering). Sequences become lists.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # Reject NaN / inf already via allow_nan=False, but be
        # defensive.
        if value != value or value in (float("inf"), float("-inf")):
            raise TypeError(f"non-finite float not canonicalizable: {value!r}")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise TypeError(
                f"naive datetime not canonicalizable: {value!r} "
                "(provide tzinfo)"
            )
        # Normalize to UTC for stability; keep microsecond precision.
        as_utc = value.astimezone(timezone.utc)
        return as_utc.isoformat(timespec="microseconds")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        # For sets, canonicalize as a sorted list. Comparable types
        # required.
        if isinstance(value, (set, frozenset)):
            try:
                ordered = sorted(value, key=_sort_key)
            except TypeError as exc:
                raise TypeError(
                    f"set with mixed/incomparable items not canonicalizable: "
                    f"{value!r}"
                ) from exc
            return [_normalize(v) for v in ordered]
        return [_normalize(v) for v in value]
    raise TypeError(
        f"value of type {type(value).__name__} not canonicalizable: {value!r}"
    )


def _sort_key(v: Any) -> tuple[int, Any]:
    """Total order for the set-canonicalization fallback path."""
    if isinstance(v, str):
        return (0, v)
    if isinstance(v, bool):
        return (1, v)
    if isinstance(v, int):
        return (2, v)
    if isinstance(v, float):
        return (3, v)
    return (4, str(v))

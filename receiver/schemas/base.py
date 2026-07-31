"""Shared field primitives for request schemas.

Two classes of malformed input reach Postgres through request bodies and, left
unchecked, surface as an unhandled 500 that loses the request rather than a
clean 4xx:

- A NUL byte (``\\x00``) in a string. Postgres text columns cannot store NUL at
  all -- it raises "invalid byte sequence for encoding UTF8: 0x00" -- so any
  string field that reaches a text column is exposed. NUL is never meaningful in
  the fields we accept, so ``NulSafeStr`` strips it at validation time.

- An integer outside the target column's range. A Postgres ``int`` is 32-bit, so
  a value past its bounds raises "integer out of range". ``Int32`` constrains a
  field to that range so the validator rejects it with a 422 instead.

These are annotated types, applied to the specific request fields that reach the
database, so a new field opts in explicitly rather than inheriting behavior it
did not ask for.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, Field

# Postgres int4 bounds. A field mapped to an INT column stays inside these so an
# out-of-range value is a validation error, not a database error.
INT32_MIN = -2_147_483_648
INT32_MAX = 2_147_483_647


def _strip_nul(value: str) -> str:
    """Remove NUL bytes from a string.

    Postgres text cannot hold ``\\x00``; stripping it here keeps a stray NUL from
    turning a write into a 500. Applied after Pydantic's own string validation,
    so length and pattern checks still see the original input.
    """
    return value.replace("\x00", "") if "\x00" in value else value


# A string with NUL bytes removed. Use for any request field that is persisted to
# a text column.
NulSafeStr = Annotated[str, AfterValidator(_strip_nul)]

# An integer constrained to the Postgres int4 range. Use for a request field that
# is persisted to an INT column and has no tighter domain bound of its own.
Int32 = Annotated[int, Field(ge=INT32_MIN, le=INT32_MAX)]

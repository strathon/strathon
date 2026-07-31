"""Malformed request input must not reach Postgres as an unhandled 500.

Two classes are closed by the shared primitives in schemas.base: a NUL byte in a
string (Postgres text cannot store it) and an integer past the int4 range. These
tests cover the primitives directly, so every field that uses them is guarded,
and spot-check the request models that adopted them.
"""

import pytest
from pydantic import BaseModel, ValidationError

from schemas.base import INT32_MAX, Int32, NulSafeStr


class _M(BaseModel):
    text: NulSafeStr = "x"
    number: Int32 = 0


def test_nul_byte_is_stripped_from_strings():
    assert _M(text="a\x00b").text == "ab"
    assert _M(text="\x00\x00lead").text == "lead"
    assert _M(text="trail\x00\x00").text == "trail"
    # A clean string is returned unchanged (no copy churn on the common path).
    assert _M(text="normal value").text == "normal value"


def test_integer_past_int32_is_rejected():
    # Inside the range is accepted.
    assert _M(number=INT32_MAX).number == INT32_MAX
    assert _M(number=-INT32_MAX).number == -INT32_MAX
    # Past it raises a validation error (surfaces as 422) rather than reaching
    # the database and raising "integer out of range" (500).
    with pytest.raises(ValidationError):
        _M(number=INT32_MAX + 1)
    with pytest.raises(ValidationError):
        _M(number=9_999_999_999)


def test_policy_create_hardens_priority_and_free_text():
    from schemas.policies import PolicyCreate

    p = PolicyCreate(
        name="a\x00b",
        match_expression="x\x00 == 1",
        action="block",
    )
    assert "\x00" not in p.name
    assert "\x00" not in p.match_expression

    with pytest.raises(ValidationError):
        PolicyCreate(
            name="ok", match_expression="1 == 1", action="block", priority=9_999_999_999
        )


def test_login_and_register_strip_nul_in_email():
    from api.auth_endpoints import LoginRequest, RegisterRequest

    assert "\x00" not in LoginRequest(email="a\x00b@x.com", password="pw").email
    reg = RegisterRequest(
        email="c\x00d@x.com", password="a-strong-password", display_name="na\x00me"
    )
    assert "\x00" not in reg.email
    assert reg.display_name is not None and "\x00" not in reg.display_name

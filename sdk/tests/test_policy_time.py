"""Tests for time-based CEL expressions: the ``now`` binding and its
timestamp methods.

CEL's spec puts time on the evaluation environment via a ``now``
variable bound to a timestamp; operators write ``now.getDayOfWeek()``,
``now.getHours()``, ``now.getDate()``, etc., and timestamp/duration
arithmetic. This matches gcloud IAM, Envoy, and KrakenD policies, so
operators familiar with CEL elsewhere don't have to learn anything new.

CEL day-of-week convention: 0 = Sunday, 1 = Monday, ..., 6 = Saturday.
This differs from Python's ``datetime.weekday()`` (Monday = 0) — worth
flagging in tests so future maintainers don't get caught.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from strathon.policy.expression import (
    PolicyExpressionError,
    clear_cache,
    evaluate,
    validate,
)


# Concrete reference timestamps used across tests. Picking known
# weekdays so the assertions are self-documenting.
SUNDAY_NOON_UTC = datetime(2024, 1, 7, 12, 0, 0, tzinfo=timezone.utc)
MONDAY_NOON_UTC = datetime(2024, 1, 8, 12, 0, 0, tzinfo=timezone.utc)
WEDNESDAY_3AM_UTC = datetime(2024, 1, 10, 3, 0, 0, tzinfo=timezone.utc)
SATURDAY_2PM_UTC = datetime(2024, 1, 13, 14, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clear_cel_cache():
    """Each test gets a fresh compile cache so leftover programs from
    one test don't mask issues in another."""
    clear_cache()
    yield
    clear_cache()


# ---- Compile-time acceptance --------------------------------------------


def test_validate_accepts_now_dayofweek_expression():
    """An operator submitting a time-based policy via the API must not
    be rejected at write time."""
    validate("now.getDayOfWeek() == 1")


def test_validate_accepts_now_hours_with_timezone():
    """Timezone-aware variants are valid CEL and must validate."""
    validate('now.getHours("America/Los_Angeles") >= 9')


def test_validate_accepts_timestamp_arithmetic_with_duration():
    validate('now - timestamp("2024-01-01T00:00:00Z") < duration("24h")')


def test_validate_accepts_combined_time_and_attribute_expression():
    """Operators typically mix time conditions with attribute checks."""
    validate(
        'name == "tool.send_email" && '
        'now.getDayOfWeek("UTC") in [0, 6]'
    )


# ---- Day-of-week semantics ----------------------------------------------


def test_now_get_day_of_week_returns_sunday_zero():
    """cel-spec: Sunday = 0 (DIFFERENT from Python's weekday() == 6 for Sunday)."""
    assert evaluate(
        "now.getDayOfWeek() == 0",
        {"name": "x", "attrs": {}},
        now=SUNDAY_NOON_UTC,
    ) is True


def test_now_get_day_of_week_returns_monday_one():
    assert evaluate(
        "now.getDayOfWeek() == 1",
        {"name": "x", "attrs": {}},
        now=MONDAY_NOON_UTC,
    ) is True


def test_now_get_day_of_week_returns_saturday_six():
    assert evaluate(
        "now.getDayOfWeek() == 6",
        {"name": "x", "attrs": {}},
        now=SATURDAY_2PM_UTC,
    ) is True


def test_block_weekend_pattern():
    """The canonical 'no agents on weekends' rule."""
    weekend_expr = "now.getDayOfWeek() == 0 || now.getDayOfWeek() == 6"
    ctx = {"name": "tool.x", "attrs": {}}
    assert evaluate(weekend_expr, ctx, now=SATURDAY_2PM_UTC) is True
    assert evaluate(weekend_expr, ctx, now=SUNDAY_NOON_UTC) is True
    assert evaluate(weekend_expr, ctx, now=MONDAY_NOON_UTC) is False


# ---- Hour-of-day semantics ----------------------------------------------


def test_now_get_hours_returns_utc_hour_by_default():
    """No-argument form returns UTC hour."""
    assert evaluate(
        "now.getHours() == 12",
        {"name": "x", "attrs": {}},
        now=MONDAY_NOON_UTC,
    ) is True


def test_now_get_hours_respects_timezone_argument():
    """``getHours('America/Los_Angeles')`` converts before extracting."""
    # 17:00 UTC = 09:00 PST (no DST) / 10:00 PDT.
    # January is PST. So 17:00 UTC -> 09:00 Pacific.
    assert evaluate(
        'now.getHours("America/Los_Angeles") == 9',
        {"name": "x", "attrs": {}},
        now=datetime(2024, 1, 8, 17, 0, 0, tzinfo=timezone.utc),
    ) is True


def test_business_hours_pacific_pattern():
    """The canonical 'business hours' rule with timezone."""
    business_hours = (
        'now.getDayOfWeek("America/Los_Angeles") >= 1 && '
        'now.getDayOfWeek("America/Los_Angeles") <= 5 && '
        'now.getHours("America/Los_Angeles") >= 9 && '
        'now.getHours("America/Los_Angeles") < 17'
    )
    ctx = {"name": "tool.x", "attrs": {}}
    # Monday 10am PST = 18:00 UTC.
    assert evaluate(
        business_hours, ctx,
        now=datetime(2024, 1, 8, 18, 0, 0, tzinfo=timezone.utc),
    ) is True
    # Wednesday 3am UTC = Tuesday 7pm PST (after hours).
    assert evaluate(business_hours, ctx, now=WEDNESDAY_3AM_UTC) is False
    # Saturday 2pm UTC = Saturday 6am PST (weekend).
    assert evaluate(business_hours, ctx, now=SATURDAY_2PM_UTC) is False


# ---- Timestamp / duration arithmetic ------------------------------------


def test_now_minus_timestamp_compared_to_duration():
    """An operator can express 'within the last hour' style conditions."""
    # MONDAY_NOON_UTC is 2024-01-08T12:00:00Z.
    # The reference timestamp is 2024-01-08T11:30:00Z, 30 minutes earlier.
    # now - reference = 30m, which is < 1h.
    expr = 'now - timestamp("2024-01-08T11:30:00Z") < duration("1h")'
    assert evaluate(expr, {"name": "x", "attrs": {}}, now=MONDAY_NOON_UTC) is True

    # Same expression with now an hour and a half later — outside the window.
    later = datetime(2024, 1, 8, 13, 0, 0, tzinfo=timezone.utc)
    assert evaluate(expr, {"name": "x", "attrs": {}}, now=later) is False


# ---- Backward compatibility ---------------------------------------------


def test_existing_attribute_only_expressions_unaffected():
    """A policy that never references ``now`` must evaluate identically
    to its pre-time-binding behavior."""
    ctx = {
        "name": "langgraph.tool.send_email",
        "attrs": {"gen_ai.tool.name": "send_email"},
    }
    assert evaluate(
        'attrs["gen_ai.tool.name"] == "send_email"', ctx,
    ) is True
    assert evaluate(
        'attrs["gen_ai.tool.name"] == "send_sms"', ctx,
    ) is False


def test_evaluate_works_without_explicit_now_argument():
    """Production callers don't pass ``now``; the function fills in the
    current UTC time. We can't pin the value, but we can check that the
    result is a sensible boolean and the call doesn't crash."""
    # We pick a tautology so the result must be True regardless of when.
    result = evaluate(
        "now.getDayOfWeek() >= 0 && now.getDayOfWeek() <= 6",
        {"name": "x", "attrs": {}},
    )
    assert result is True


# ---- Defensive behavior -------------------------------------------------


def test_naive_datetime_promoted_to_utc():
    """A timezone-naive datetime would otherwise be interpreted as local
    time by celpy's underlying conversion, breaking getDayOfWeek/getHours.
    Promote to UTC so the policy sees what the caller meant."""
    # 2024-01-08T12:00:00 NAIVE — should be treated as UTC noon Monday.
    naive_monday_noon = datetime(2024, 1, 8, 12, 0, 0)
    assert evaluate(
        "now.getDayOfWeek() == 1 && now.getHours() == 12",
        {"name": "x", "attrs": {}},
        now=naive_monday_noon,
    ) is True


def test_invalid_time_expression_returns_false_not_raises():
    """A policy that typos a method name evaluates to False and logs,
    matching the existing fail-safe contract for runtime errors."""
    # getDay does not exist; spec is getDate / getDayOfWeek / etc.
    # Compile succeeds (gradual typing); evaluate fails at runtime.
    result = evaluate(
        "now.getDay() == 1",
        {"name": "x", "attrs": {}},
        now=MONDAY_NOON_UTC,
    )
    assert result is False


def test_malformed_time_expression_caught_at_validate():
    """Genuinely malformed CEL (parse error) is still rejected at
    validate time."""
    with pytest.raises(PolicyExpressionError):
        validate("now.getDayOfWeek( ==")

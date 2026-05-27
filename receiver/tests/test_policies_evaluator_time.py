"""Tests for time-based CEL on the receiver's policy evaluator.

Mirrors the SDK suite in ``sdk/tests/test_policy_time.py``. The
receiver vendors its own copy of the CEL evaluator (Apache vs MIT
licensing means we can't share the module across packages), so this
file proves the two implementations stay in sync.

The receiver's evaluator is called from two paths: the policy CRUD
layer's ``validate_expression`` at write time, and the ingest hot
path's per-span ``evaluate`` for matching.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest


_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)


from policies_eval import (  # noqa: E402
    PolicyExpressionError,
    evaluate,
    validate_expression,
    _COMPILE_CACHE,
)


SUNDAY_NOON_UTC = datetime(2024, 1, 7, 12, 0, 0, tzinfo=timezone.utc)
MONDAY_NOON_UTC = datetime(2024, 1, 8, 12, 0, 0, tzinfo=timezone.utc)
SATURDAY_2PM_UTC = datetime(2024, 1, 13, 14, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clear_compile_cache():
    _COMPILE_CACHE.clear()
    yield
    _COMPILE_CACHE.clear()


# ---- validate_expression accepts time-based syntax ----------------------


def test_validate_expression_accepts_now_dayofweek():
    """The policy CRUD layer must accept time-based rules at write time
    so operators don't have to discover the feature exists by trial."""
    validate_expression("now.getDayOfWeek() == 1")


def test_validate_expression_accepts_now_hours_with_timezone():
    validate_expression('now.getHours("America/Los_Angeles") >= 9')


def test_validate_expression_accepts_timestamp_arithmetic():
    validate_expression('now - timestamp("2024-01-01T00:00:00Z") < duration("24h")')


def test_validate_expression_rejects_malformed_time_syntax():
    with pytest.raises(PolicyExpressionError):
        validate_expression("now.getDayOfWeek( ==")


# ---- evaluate binds 'now' and produces correct results ------------------


def test_evaluate_now_get_day_of_week_sunday_zero():
    """cel-spec: Sunday = 0. Pin the time deterministically."""
    assert evaluate(
        "now.getDayOfWeek() == 0",
        {"name": "x", "attrs": {}},
        now=SUNDAY_NOON_UTC,
    ) is True


def test_evaluate_now_get_day_of_week_monday_one():
    assert evaluate(
        "now.getDayOfWeek() == 1",
        {"name": "x", "attrs": {}},
        now=MONDAY_NOON_UTC,
    ) is True


def test_evaluate_block_weekend_pattern():
    expr = "now.getDayOfWeek() == 0 || now.getDayOfWeek() == 6"
    ctx = {"name": "tool.x", "attrs": {}}
    assert evaluate(expr, ctx, now=SATURDAY_2PM_UTC) is True
    assert evaluate(expr, ctx, now=SUNDAY_NOON_UTC) is True
    assert evaluate(expr, ctx, now=MONDAY_NOON_UTC) is False


def test_evaluate_business_hours_pacific():
    expr = (
        'now.getDayOfWeek("America/Los_Angeles") >= 1 && '
        'now.getDayOfWeek("America/Los_Angeles") <= 5 && '
        'now.getHours("America/Los_Angeles") >= 9 && '
        'now.getHours("America/Los_Angeles") < 17'
    )
    ctx = {"name": "tool.x", "attrs": {}}
    # Monday 10am PST = 18:00 UTC.
    assert evaluate(
        expr, ctx,
        now=datetime(2024, 1, 8, 18, 0, 0, tzinfo=timezone.utc),
    ) is True
    # Saturday — fails day-of-week even though hour is right.
    assert evaluate(expr, ctx, now=SATURDAY_2PM_UTC) is False


# ---- Backward compatibility ---------------------------------------------


def test_evaluate_existing_attribute_expression_unaffected():
    """A policy that never mentions ``now`` must evaluate identically to
    its pre-time-binding behavior."""
    ctx = {
        "name": "langgraph.tool.send_email",
        "attrs": {"gen_ai.tool.name": "send_email"},
    }
    assert evaluate('attrs["gen_ai.tool.name"] == "send_email"', ctx) is True
    assert evaluate('attrs["gen_ai.tool.name"] == "send_sms"', ctx) is False


def test_evaluate_without_explicit_now_argument():
    """Production callers don't pass ``now``; the evaluator fills in
    current UTC. We pick a tautology to assert this works."""
    result = evaluate(
        "now.getDayOfWeek() >= 0 && now.getDayOfWeek() <= 6",
        {"name": "x", "attrs": {}},
    )
    assert result is True


# ---- Defensive ----------------------------------------------------------


def test_naive_datetime_promoted_to_utc():
    naive_monday_noon = datetime(2024, 1, 8, 12, 0, 0)
    assert evaluate(
        "now.getDayOfWeek() == 1 && now.getHours() == 12",
        {"name": "x", "attrs": {}},
        now=naive_monday_noon,
    ) is True


def test_runtime_typo_returns_false_does_not_raise():
    """A method that doesn't exist on TimestampType compiles (gradual
    typing) but fails at evaluate. The receiver MUST NOT crash on the
    ingest path; falling through to False matches the existing contract."""
    result = evaluate(
        "now.getDay() == 1",
        {"name": "x", "attrs": {}},
        now=MONDAY_NOON_UTC,
    )
    assert result is False

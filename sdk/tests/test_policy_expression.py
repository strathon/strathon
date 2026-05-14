"""Tests for the CEL-based policy match expression evaluator."""

import pytest

from strathon.policy import PolicyExpressionError, evaluate, validate
from strathon.policy.expression import clear_cache


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test gets a fresh compile cache so we don't leak state across tests."""
    clear_cache()
    yield
    clear_cache()


def make_span(
    name="crewai.tool.web_search",
    tool_name="web_search",
    tool_args=None,
    model="gpt-4o",
    total_tokens=0,
    framework="crewai",
):
    return {
        "name": name,
        "attrs": {
            "strathon.framework": framework,
            "gen_ai.tool.name": tool_name,
            "strathon.tool.args": tool_args or '{"query": "x"}',
            "gen_ai.request.model": model,
            "gen_ai.usage.total_tokens": total_tokens,
        },
    }


# ---- Basic comparisons ----


def test_eq_matches_tool_name():
    span = make_span(tool_name="send_email")
    assert evaluate('attrs["gen_ai.tool.name"] == "send_email"', span) is True


def test_eq_does_not_match_when_different():
    span = make_span(tool_name="web_search")
    assert evaluate('attrs["gen_ai.tool.name"] == "send_email"', span) is False


def test_ne_inverts_eq():
    span = make_span(tool_name="web_search")
    assert evaluate('attrs["gen_ai.tool.name"] != "send_email"', span) is True


def test_name_field_is_accessible():
    span = make_span(name="crewai.tool.email")
    assert evaluate('name == "crewai.tool.email"', span) is True


# ---- String operators ----


def test_contains_matches_substring():
    span = make_span(tool_args='{"to": "rival@competitor.com"}')
    assert (
        evaluate(
            'attrs["strathon.tool.args"].contains("competitor.com")',
            span,
        )
        is True
    )


def test_startswith():
    span = make_span(name="crewai.tool.web_search")
    assert evaluate('name.startsWith("crewai.tool")', span) is True
    assert evaluate('name.startsWith("langgraph")', span) is False


def test_endswith():
    span = make_span(name="crewai.tool.web_search")
    assert evaluate('name.endsWith("web_search")', span) is True
    assert evaluate('name.endsWith("agent")', span) is False


# ---- Numeric comparisons ----


def test_gt_matches_when_above_threshold():
    span = make_span(total_tokens=1500)
    assert evaluate('attrs["gen_ai.usage.total_tokens"] > 1000', span) is True


def test_gt_does_not_match_at_or_below():
    span = make_span(total_tokens=1000)
    assert evaluate('attrs["gen_ai.usage.total_tokens"] > 1000', span) is False


def test_combined_comparisons():
    span = make_span(total_tokens=500)
    assert evaluate('attrs["gen_ai.usage.total_tokens"] >= 500', span) is True
    assert evaluate('attrs["gen_ai.usage.total_tokens"] <= 500', span) is True
    assert evaluate('attrs["gen_ai.usage.total_tokens"] < 500', span) is False


# ---- Logical operators ----


def test_logical_and():
    span = make_span(
        tool_name="send_email", tool_args='{"to": "evil@competitor.com"}'
    )
    expr = (
        'attrs["gen_ai.tool.name"] == "send_email" && '
        'attrs["strathon.tool.args"].contains("competitor.com")'
    )
    assert evaluate(expr, span) is True


def test_logical_and_short_circuit():
    span = make_span(tool_name="web_search")
    expr = (
        'attrs["gen_ai.tool.name"] == "send_email" && '
        'attrs["strathon.tool.args"].contains("competitor.com")'
    )
    assert evaluate(expr, span) is False


def test_logical_or():
    span = make_span(tool_name="web_search")
    expr = (
        'attrs["gen_ai.tool.name"] == "send_email" || '
        'attrs["gen_ai.tool.name"] == "web_search"'
    )
    assert evaluate(expr, span) is True


def test_not_operator():
    span = make_span(tool_name="web_search")
    assert evaluate('!(attrs["gen_ai.tool.name"] == "send_email")', span) is True


# ---- `in` operator ----


def test_in_list():
    span = make_span(model="gpt-4o")
    assert (
        evaluate(
            'attrs["gen_ai.request.model"] in ["gpt-4o", "claude-3"]', span
        )
        is True
    )


def test_in_list_no_match():
    span = make_span(model="mistral")
    assert (
        evaluate(
            'attrs["gen_ai.request.model"] in ["gpt-4o", "claude-3"]', span
        )
        is False
    )


# ---- Real-world flagship rules ----


def test_block_competitor_emails():
    expr = (
        'attrs["gen_ai.tool.name"] == "send_email" && '
        'attrs["strathon.tool.args"].contains("@competitor.com")'
    )
    bad = make_span(
        tool_name="send_email",
        tool_args='{"to": "sales@competitor.com", "body": "hi"}',
    )
    good = make_span(
        tool_name="send_email",
        tool_args='{"to": "boss@mycompany.com", "body": "hi"}',
    )
    assert evaluate(expr, bad) is True
    assert evaluate(expr, good) is False


def test_alert_on_expensive_llm_calls():
    expr = (
        '(name.startsWith("crewai.llm") || '
        'name.startsWith("langgraph.llm") || '
        'name.startsWith("agents.generation")) && '
        'attrs["gen_ai.usage.total_tokens"] > 5000'
    )
    expensive = make_span(name="crewai.llm", total_tokens=10000)
    cheap = make_span(name="crewai.llm", total_tokens=100)
    not_llm = make_span(name="crewai.tool.web_search", total_tokens=10000)
    assert evaluate(expr, expensive) is True
    assert evaluate(expr, cheap) is False
    assert evaluate(expr, not_llm) is False


# ---- Robustness ----


def test_malformed_expression_returns_false():
    """We choose silent-deny over crashing."""
    span = make_span()
    assert evaluate("@@@ not valid", span) is False
    assert evaluate("", span) is False
    assert evaluate(None, span) is False


def test_missing_attr_does_not_crash():
    """Accessing a missing key should evaluate as False, not raise."""
    span = make_span()
    # The attribute doesn't exist in our span
    assert evaluate('attrs["does.not.exist"] == "anything"', span) is False


def test_empty_span_context():
    span = {}
    # Should not crash; just returns False for missing data
    assert evaluate('name == "anything"', span) is False


# ---- Compile cache ----


def test_compile_cache_speeds_up_repeated_eval():
    """Second eval of the same expression should reuse the compiled program."""
    span = make_span(tool_name="send_email")
    expr = 'attrs["gen_ai.tool.name"] == "send_email"'
    assert evaluate(expr, span) is True
    # Re-evaluate; the compile cache should have it.
    assert evaluate(expr, span) is True
    from strathon.policy.expression import _COMPILE_CACHE
    assert expr in _COMPILE_CACHE


# ---- validate() ----


def test_validate_accepts_well_formed_expression():
    validate('attrs["gen_ai.tool.name"] == "send_email"')
    validate('name.startsWith("crewai") && attrs["x"] > 0')


def test_validate_rejects_malformed():
    with pytest.raises(PolicyExpressionError):
        validate("not valid @@@")
    with pytest.raises(PolicyExpressionError):
        validate("")
    with pytest.raises(PolicyExpressionError):
        validate(None)
    with pytest.raises(PolicyExpressionError):
        validate(123)


def test_validate_does_not_require_actual_data():
    """validate is compile-only; it shouldn't need a span context."""
    # Refers to attrs/name without us providing them — should still compile fine
    validate('attrs["whatever"] == "anything"')

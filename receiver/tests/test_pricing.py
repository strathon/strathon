"""Tests for the pricing module.

Coverage:
  * Catalog loads with expected models
  * compute_cost_usd math is correct for known models
  * Decimal precision survives through the arithmetic
  * Unknown model returns None (not 0)
  * Missing model name returns None
  * Zero tokens returns None
  * Per-project overrides take priority over catalog
  * Malformed catalog entries are skipped without crashing
"""

from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from pricing import (
    ModelPrice,
    compute_cost_usd,
    get_default_catalog,
    load_catalog,
    reset_catalog_for_testing,
)


@pytest.fixture(autouse=True)
def _reset_catalog():
    """Clear the module-level cache between tests."""
    reset_catalog_for_testing()
    yield
    reset_catalog_for_testing()


# ---- Catalog loading -------------------------------------------------


def test_default_catalog_loads_expected_models():
    catalog = get_default_catalog()
    # We ship 20 in the vendored JSON; test ensures the loader sees
    # the headline OpenAI / Anthropic / Google entries.
    assert "gpt-4o" in catalog
    assert "gpt-4o-mini" in catalog
    assert "claude-3-5-sonnet-20241022" in catalog
    assert "gemini-1.5-pro" in catalog


def test_catalog_skips_meta_and_malformed_entries():
    fake = {
        "_meta": {"version": 1},  # should be skipped
        "good-model": {
            "input_cost_per_token": 0.00001,
            "output_cost_per_token": 0.00003,
        },
        "missing-output-price": {
            "input_cost_per_token": 0.00001,
            # no output_cost_per_token → skipped
        },
        "not-a-dict": "weird",  # → skipped
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(fake, f)
        fpath = Path(f.name)

    catalog = load_catalog(fpath)
    assert "good-model" in catalog
    assert "_meta" not in catalog
    assert "missing-output-price" not in catalog
    assert "not-a-dict" not in catalog


def test_catalog_missing_file_returns_empty():
    catalog = load_catalog(Path("/tmp/definitely-does-not-exist-12345.json"))
    assert catalog == {}


def test_catalog_malformed_json_returns_empty():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("not valid json {{{")
        fpath = Path(f.name)
    catalog = load_catalog(fpath)
    assert catalog == {}


# ---- Cost computation ------------------------------------------------


def test_gpt4o_cost_math():
    """1000 input + 500 output tokens on gpt-4o:
       1000 * 0.0000025 + 500 * 0.00001 = 0.0025 + 0.005 = 0.0075
    """
    catalog = get_default_catalog()
    cost = compute_cost_usd(
        model_name="gpt-4o",
        input_tokens=1000,
        output_tokens=500,
        catalog=catalog,
    )
    assert cost == Decimal("0.00750000")


def test_claude_sonnet_cost_math():
    """100 input + 200 output on claude-3-5-sonnet:
       100 * 0.000003 + 200 * 0.000015 = 0.0003 + 0.003 = 0.0033
    """
    catalog = get_default_catalog()
    cost = compute_cost_usd(
        model_name="claude-3-5-sonnet-20241022",
        input_tokens=100,
        output_tokens=200,
        catalog=catalog,
    )
    assert cost == Decimal("0.00330000")


def test_unknown_model_returns_none():
    """Unknown models must return None (not 0) so dashboards can
    surface 'unknown' rather than silently misattribute spend."""
    catalog = get_default_catalog()
    cost = compute_cost_usd(
        model_name="some-fictional-model-v9",
        input_tokens=1000,
        output_tokens=500,
        catalog=catalog,
    )
    assert cost is None


def test_missing_model_name_returns_none():
    catalog = get_default_catalog()
    cost = compute_cost_usd(
        model_name=None,
        input_tokens=1000,
        output_tokens=500,
        catalog=catalog,
    )
    assert cost is None


def test_zero_tokens_returns_none():
    """A span with zero tokens recorded isn't free — it likely means
    we didn't capture the tokens. Returning None vs 0 lets dashboards
    distinguish 'no LLM call' from 'free LLM call'."""
    catalog = get_default_catalog()
    cost = compute_cost_usd(
        model_name="gpt-4o",
        input_tokens=0,
        output_tokens=0,
        catalog=catalog,
    )
    assert cost is None


def test_input_only_or_output_only_still_charges():
    """A span with input tokens but no output (streaming start record)
    still has a cost. Same for the rarer output-only case."""
    catalog = get_default_catalog()
    cost = compute_cost_usd(
        model_name="gpt-4o",
        input_tokens=1000,
        output_tokens=None,  # explicit None
        catalog=catalog,
    )
    # 1000 * 0.0000025 = 0.0025
    assert cost == Decimal("0.00250000")

    cost = compute_cost_usd(
        model_name="gpt-4o",
        input_tokens=None,
        output_tokens=500,
        catalog=catalog,
    )
    # 500 * 0.00001 = 0.005
    assert cost == Decimal("0.00500000")


def test_overrides_take_priority_over_catalog():
    """Operator-set override beats the vendored catalog for the same
    model. The catalog default is gpt-4o at $0.0000025 input;
    override at $0.00001 input means 1000 tokens cost $0.01."""
    catalog = get_default_catalog()
    overrides = {
        "gpt-4o": ModelPrice(
            input_cost_per_token=Decimal("0.00001"),
            output_cost_per_token=Decimal("0.00002"),
        )
    }
    cost = compute_cost_usd(
        model_name="gpt-4o",
        input_tokens=1000,
        output_tokens=500,
        catalog=catalog,
        overrides=overrides,
    )
    # 1000 * 0.00001 + 500 * 0.00002 = 0.01 + 0.01 = 0.02
    assert cost == Decimal("0.02000000")


def test_overrides_for_unknown_model_still_compute():
    """Override for a model not in the catalog still gives a cost.
    This is how operators can budget for self-hosted or fine-tuned
    models that aren't in LiteLLM's upstream."""
    catalog = get_default_catalog()
    overrides = {
        "my-llama-3-70b-self-hosted": ModelPrice(
            input_cost_per_token=Decimal("0.000001"),
            output_cost_per_token=Decimal("0.000001"),
        )
    }
    cost = compute_cost_usd(
        model_name="my-llama-3-70b-self-hosted",
        input_tokens=1000,
        output_tokens=1000,
        catalog=catalog,
        overrides=overrides,
    )
    # 1000 * 0.000001 + 1000 * 0.000001 = 0.001 + 0.001 = 0.002
    assert cost == Decimal("0.00200000")


def test_decimal_precision_no_drift_across_many_spans():
    """Sum of many small cost values should be exact, not float-drifted.
    The naive `float(cost) for cost in spans; sum(...)` approach drifts
    by the 1000-span mark. Decimal arithmetic keeps it exact."""
    catalog = get_default_catalog()
    one_span = compute_cost_usd(
        model_name="gemini-1.5-flash",
        input_tokens=100,
        output_tokens=100,
        catalog=catalog,
    )
    # One span: 100 * 0.000000075 + 100 * 0.0000003 = 0.0000075 + 0.00003
    # = 0.0000375
    assert one_span == Decimal("0.00003750")

    total = sum(
        (compute_cost_usd(
            model_name="gemini-1.5-flash",
            input_tokens=100, output_tokens=100,
            catalog=catalog,
        ) for _ in range(1000)),
        start=Decimal("0"),
    )
    # 1000 * 0.0000375 = 0.0375, exact
    assert total == Decimal("0.03750000")

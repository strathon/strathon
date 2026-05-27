"""Per-span LLM cost computation.

Strathon stores the dollar cost of each LLM span as a column on the
spans table at ingest time. The number is derived from
``gen_ai.usage.input_tokens`` + ``gen_ai.usage.output_tokens`` against a
per-model price catalog.

Why on-the-span vs counter
==========================

The naive design ("UPDATE budgets SET spent = spent + cost on every
ingest") creates a hot-row contention point: every span ingest for the
same project serializes on one row. At anything past a few hundred
spans/second this becomes the bottleneck, with each writer waiting for
the previous one's lock.

The industry-standard pattern for LLM cost tracking is: write the
cost on the span itself, aggregate at read time. Spans table inserts
don't contend with each other; the aggregation is a windowed
``SUM(cost_usd)`` over an indexed range, which Postgres handles in
milliseconds for normal volumes.

This also composes naturally: per-trace cost, per-agent cost, per-model
cost are all the same aggregation with different GROUP BY clauses. No
schema or code changes needed to support new dimensions.

Pricing source
==============

The catalog is a vendored JSON file at
``receiver/pricing/model_prices.json``, sourced from LiteLLM's
upstream model_prices_and_context_window.json (MIT-licensed). v1 ships
~20 of the most-used models; the file gets refreshed periodically via
a sync script. We deliberately do NOT pull at runtime: a network fetch
on receiver startup means the budget feature is broken when GitHub is
slow, and version-pinning the file in the repo keeps cost calculations
deterministic across deployments.

Operators override prices per-project per-model via the
``model_price_overrides`` table. Lookup order is:

    1. project_id+model_name in model_price_overrides   (operator's value wins)
    2. model_name in the vendored catalog               (default)
    3. None                                             (unknown model -> cost is None)

A span with an unknown model gets cost_usd = NULL rather than 0. This
is intentional: 0 would silently misattribute spend, while NULL is
visible (it shows up in dashboards as "unknown") and the operator can
fix it by adding an override.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("strathon.receiver.pricing")


_CATALOG_PATH = Path(__file__).parent / "data" / "model_prices.json"


@dataclass(frozen=True)
class ModelPrice:
    """Per-token cost in USD. Decimal because cost arithmetic needs
    precision (sums of many small numbers); float drifts by the time
    you've ingested a million spans."""

    input_cost_per_token: Decimal
    output_cost_per_token: Decimal
    provider: Optional[str] = None
    mode: Optional[str] = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "ModelPrice":
        return cls(
            input_cost_per_token=Decimal(str(data["input_cost_per_token"])),
            output_cost_per_token=Decimal(str(data["output_cost_per_token"])),
            provider=data.get("litellm_provider"),
            mode=data.get("mode"),
        )


# ---- Catalog loading ---------------------------------------------------


_CATALOG: Optional[dict[str, ModelPrice]] = None


def load_catalog(path: Path = _CATALOG_PATH) -> dict[str, ModelPrice]:
    """Read and parse the vendored model_prices.json.

    Cached at module level after first call. Tests can call this with
    a different path to swap in fixtures. The cache is intentionally
    process-level (not request-level): the catalog is immutable for
    the lifetime of the receiver process, refresh requires restart.
    Operator overrides live in the DB and don't go through this cache.
    """
    global _CATALOG
    if _CATALOG is not None and path == _CATALOG_PATH:
        return _CATALOG

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        logger.warning(
            "Model price catalog not found at %s; cost computation disabled",
            path,
        )
        if path == _CATALOG_PATH:
            _CATALOG = {}
        return {}
    except json.JSONDecodeError as exc:
        logger.error("Model price catalog at %s is malformed: %s", path, exc)
        if path == _CATALOG_PATH:
            _CATALOG = {}
        return {}

    catalog: dict[str, ModelPrice] = {}
    for model_name, entry in raw.items():
        if model_name.startswith("_"):
            continue  # skip _meta and any future _-prefixed sections
        if not isinstance(entry, dict):
            continue
        if "input_cost_per_token" not in entry or "output_cost_per_token" not in entry:
            # Some entries in upstream don't have token-based pricing
            # (e.g. character-based, embedding models). Skip them rather
            # than crashing on missing keys.
            continue
        try:
            catalog[model_name] = ModelPrice.from_json(entry)
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "Skipping malformed catalog entry for %r: %s", model_name, exc,
            )

    if path == _CATALOG_PATH:
        _CATALOG = catalog
    logger.info("Loaded %d models from price catalog at %s", len(catalog), path)
    return catalog


def reset_catalog_for_testing() -> None:
    """Tests that swap the catalog should call this to clear the cache."""
    global _CATALOG
    _CATALOG = None


# ---- Per-project overrides --------------------------------------------


async def get_project_overrides(
    session: AsyncSession,
    project_id: UUID,
) -> dict[str, ModelPrice]:
    """Load per-project price overrides from the DB.

    Returns a dict matching the catalog shape. Empty dict if the
    project has no overrides (which is the default; most projects use
    the vendored catalog as-is).

    NOTE: We don't cache this in-process across requests. Operator
    edits via the REST API need to take effect immediately, and the
    overrides table is small enough per project that the read is
    cheap. If profiling shows this hot, the right cache layer is a
    short TTL keyed on project_id; LRU is wrong because multi-receiver
    deploys would diverge.
    """
    from models.intervention import ModelPriceOverride

    result = await session.scalars(
        select(ModelPriceOverride).where(
            ModelPriceOverride.project_id == project_id,
        )
    )
    out: dict[str, ModelPrice] = {}
    for row in result.all():
        out[row.model_name] = ModelPrice(
            input_cost_per_token=Decimal(row.input_cost_per_token),
            output_cost_per_token=Decimal(row.output_cost_per_token),
            provider=None,
            mode=None,
        )
    return out


# ---- Cost computation -------------------------------------------------


def compute_cost_usd(
    *,
    model_name: Optional[str],
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    catalog: Mapping[str, ModelPrice],
    overrides: Optional[Mapping[str, ModelPrice]] = None,
) -> Optional[Decimal]:
    """Compute the dollar cost for one LLM span.

    Returns None when:
      * model_name is missing (non-LLM span: tool call, generic op)
      * model_name isn't in the catalog or overrides
      * both token counts are missing or zero

    Returning None vs 0 matters: 0 would silently misattribute spend,
    making "cost by model" charts wrong. None surfaces as "unknown" and
    operators can fix it with an override.

    The arithmetic uses Decimal end-to-end to avoid float drift.
    """
    if not model_name:
        return None

    price: Optional[ModelPrice] = None
    if overrides is not None and model_name in overrides:
        price = overrides[model_name]
    elif model_name in catalog:
        price = catalog[model_name]
    if price is None:
        return None

    in_tok = input_tokens or 0
    out_tok = output_tokens or 0
    if in_tok == 0 and out_tok == 0:
        return None

    total = (
        price.input_cost_per_token * Decimal(in_tok)
        + price.output_cost_per_token * Decimal(out_tok)
    )
    # Quantize to 8 decimal places to match the DB column.
    return total.quantize(Decimal("0.00000001"))


# ---- Convenience accessor used by ingest -------------------------------


_ENV_CATALOG_PATH = os.environ.get("STRATHON_MODEL_PRICES_PATH")


def get_default_catalog() -> dict[str, ModelPrice]:
    """The catalog used by the ingest path. Operators can override the
    path via STRATHON_MODEL_PRICES_PATH (useful for air-gapped envs
    that ship their own JSON)."""
    if _ENV_CATALOG_PATH:
        return load_catalog(Path(_ENV_CATALOG_PATH))
    return load_catalog()


__all__ = [
    "ModelPrice",
    "compute_cost_usd",
    "get_default_catalog",
    "get_project_overrides",
    "load_catalog",
    "reset_catalog_for_testing",
]

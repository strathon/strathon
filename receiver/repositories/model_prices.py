"""Per-project model price overrides repository.

The vendored catalog at ``receiver/data/model_prices.json`` is the
default price for every model. Operators who want different prices —
typically because they've negotiated a discount with their provider —
write rows here. The cost computation at ingest checks this table
first per project, then falls back to the vendored catalog.

Why a dedicated table (vs JSONB column on projects)
====================================================

Three reasons:

1. Unique constraint per (project, model) means SQL enforces "at most
   one override per model per project" — operators can't accidentally
   double-create.

2. The aggregation paths (cost calculator, budget monitor) want to
   join against a normalized shape, not parse a JSONB blob per request.

3. The override surface evolves. v1 ships per-token pricing; v2 might
   add per-character or per-image-tile pricing for multimodal models.
   A table makes those new columns a straightforward migration; a
   JSONB blob makes them undiscoverable.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.intervention import ModelPriceOverride

logger = logging.getLogger("strathon.receiver.repositories.model_prices")


@dataclass(frozen=True)
class PriceOverrideRow:
    id: uuid.UUID
    project_id: uuid.UUID
    model_name: str
    input_cost_per_token: Decimal
    output_cost_per_token: Decimal
    created_at: datetime
    updated_at: datetime

    def to_json(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "model_name": self.model_name,
            "input_cost_per_token": str(self.input_cost_per_token),
            "output_cost_per_token": str(self.output_cost_per_token),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def _row_to_dto(row: ModelPriceOverride) -> PriceOverrideRow:
    return PriceOverrideRow(
        id=row.id,
        project_id=row.project_id,
        model_name=row.model_name,
        input_cost_per_token=row.input_cost_per_token,
        output_cost_per_token=row.output_cost_per_token,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def upsert_override(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    model_name: str,
    input_cost_per_token: Decimal,
    output_cost_per_token: Decimal,
) -> PriceOverrideRow:
    """Create or update an override.

    The endpoint contract is upsert (POST /v1/model_prices is
    idempotent on model_name): operators editing a price expect the
    second POST to replace the first, not to 409. The DB unique
    constraint is the safety net; the application code does the
    upsert explicitly.

    Validation: prices must be non-negative (also enforced by DB
    CHECK constraint, but raise a friendlier error here).
    """
    if not model_name or not model_name.strip():
        raise ValueError("model_name is required")
    if input_cost_per_token < 0 or output_cost_per_token < 0:
        raise ValueError("prices must be non-negative")

    model_name = model_name.strip()

    existing = await session.scalar(
        select(ModelPriceOverride).where(
            ModelPriceOverride.project_id == project_id,
            ModelPriceOverride.model_name == model_name,
        )
    )
    if existing is not None:
        await session.execute(
            update(ModelPriceOverride)
            .where(ModelPriceOverride.id == existing.id)
            .values(
                input_cost_per_token=input_cost_per_token,
                output_cost_per_token=output_cost_per_token,
            )
        )
        refreshed = await session.scalar(
            select(ModelPriceOverride).where(ModelPriceOverride.id == existing.id)
        )
        assert refreshed is not None, (
            f"model price override {existing.id} vanished mid-transaction"
        )
        logger.info(
            "Updated price override for %s in project %s", model_name, project_id,
        )
        return _row_to_dto(refreshed)

    try:
        result = await session.execute(
            insert(ModelPriceOverride)
            .values(
                project_id=project_id,
                model_name=model_name,
                input_cost_per_token=input_cost_per_token,
                output_cost_per_token=output_cost_per_token,
            )
            .returning(ModelPriceOverride)
        )
        row = result.scalar_one()
    except IntegrityError:
        # Race: another transaction inserted between our SELECT and
        # INSERT. Re-read and return that one. This matches the
        # upsert contract.
        await session.rollback()
        existing = await session.scalar(
            select(ModelPriceOverride).where(
                ModelPriceOverride.project_id == project_id,
                ModelPriceOverride.model_name == model_name,
            )
        )
        if existing is None:
            raise  # shouldn't happen, but don't swallow silently
        return _row_to_dto(existing)

    logger.info(
        "Created price override for %s in project %s", model_name, project_id,
    )
    return _row_to_dto(row)


async def list_overrides(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[PriceOverrideRow]:
    """List all overrides for a project, alphabetical by model name."""
    result = await session.scalars(
        select(ModelPriceOverride)
        .where(ModelPriceOverride.project_id == project_id)
        .order_by(ModelPriceOverride.model_name.asc())
    )
    return [_row_to_dto(r) for r in result.all()]


async def delete_override(
    session: AsyncSession,
    project_id: uuid.UUID,
    model_name: str,
) -> bool:
    """Remove an override. Returns True if a row was deleted, False
    if no override existed for that model."""
    result = await session.execute(
        delete(ModelPriceOverride).where(
            ModelPriceOverride.project_id == project_id,
            ModelPriceOverride.model_name == model_name,
        )
    )
    # rowcount is exposed by the runtime CursorResult; the SQLAlchemy
    # 2.x protocol type hides it.
    return result.rowcount > 0  # type: ignore[attr-defined]


__all__ = [
    "PriceOverrideRow",
    "delete_override",
    "list_overrides",
    "upsert_override",
]

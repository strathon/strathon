"""Tests for the model_prices override repository.

Coverage:
  * upsert creates a new row
  * upsert on existing (project, model) updates in place — not duplicate
  * list returns alphabetical
  * delete returns True/False appropriately
  * Cross-project isolation
  * Negative prices rejected
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import insert as sa_insert

import repositories.model_prices as prices_repo


# ---- Fixtures ---------------------------------------------------------


@pytest_asyncio.fixture
async def second_project(session):
    from models import Project, ProjectSettings
    pid = uuid.uuid4()
    await session.execute(
        sa_insert(Project).values(
            id=pid, name=f"Other {pid.hex[:6]}", slug=f"other-{pid.hex[:8]}",
        )
    )
    await session.execute(sa_insert(ProjectSettings).values(project_id=pid))
    await session.flush()
    return pid


# ---- upsert ----------------------------------------------------------


async def test_upsert_creates_new_override(session, isolated_project):
    row = await prices_repo.upsert_override(
        session, isolated_project,
        model_name="gpt-4o",
        input_cost_per_token=Decimal("0.000001"),
        output_cost_per_token=Decimal("0.000002"),
    )
    assert row.model_name == "gpt-4o"
    assert row.input_cost_per_token == Decimal("0.000001")
    assert row.output_cost_per_token == Decimal("0.000002")


async def test_upsert_updates_existing(session, isolated_project):
    await prices_repo.upsert_override(
        session, isolated_project,
        model_name="gpt-4o",
        input_cost_per_token=Decimal("0.000001"),
        output_cost_per_token=Decimal("0.000002"),
    )
    # Second upsert with new prices — should update, not insert duplicate
    row2 = await prices_repo.upsert_override(
        session, isolated_project,
        model_name="gpt-4o",
        input_cost_per_token=Decimal("0.000010"),
        output_cost_per_token=Decimal("0.000020"),
    )
    assert row2.input_cost_per_token == Decimal("0.000010")

    # And there's only one row in the table for this project
    rows = await prices_repo.list_overrides(session, isolated_project)
    assert len(rows) == 1
    assert rows[0].input_cost_per_token == Decimal("0.000010")


async def test_upsert_rejects_negative_price(session, isolated_project):
    with pytest.raises(ValueError, match="non-negative"):
        await prices_repo.upsert_override(
            session, isolated_project,
            model_name="gpt-4o",
            input_cost_per_token=Decimal("-0.001"),
            output_cost_per_token=Decimal("0.001"),
        )


async def test_upsert_rejects_empty_model_name(session, isolated_project):
    with pytest.raises(ValueError, match="required"):
        await prices_repo.upsert_override(
            session, isolated_project,
            model_name="",
            input_cost_per_token=Decimal("0.001"),
            output_cost_per_token=Decimal("0.001"),
        )


# ---- list / delete ---------------------------------------------------


async def test_list_returns_overrides_alphabetical(session, isolated_project):
    for model in ("gpt-4o", "claude-3-haiku", "gemini-1.5-flash"):
        await prices_repo.upsert_override(
            session, isolated_project,
            model_name=model,
            input_cost_per_token=Decimal("0.001"),
            output_cost_per_token=Decimal("0.001"),
        )
    rows = await prices_repo.list_overrides(session, isolated_project)
    names = [r.model_name for r in rows]
    assert names == sorted(names)


async def test_list_cross_project_isolation(
    session, isolated_project, second_project,
):
    await prices_repo.upsert_override(
        session, isolated_project,
        model_name="gpt-4o",
        input_cost_per_token=Decimal("0.001"),
        output_cost_per_token=Decimal("0.001"),
    )
    await prices_repo.upsert_override(
        session, second_project,
        model_name="claude-3-5-sonnet",
        input_cost_per_token=Decimal("0.001"),
        output_cost_per_token=Decimal("0.001"),
    )

    mine = await prices_repo.list_overrides(session, isolated_project)
    theirs = await prices_repo.list_overrides(session, second_project)
    assert [r.model_name for r in mine] == ["gpt-4o"]
    assert [r.model_name for r in theirs] == ["claude-3-5-sonnet"]


async def test_delete_returns_true_for_existing(session, isolated_project):
    await prices_repo.upsert_override(
        session, isolated_project,
        model_name="gpt-4o",
        input_cost_per_token=Decimal("0.001"),
        output_cost_per_token=Decimal("0.001"),
    )
    deleted = await prices_repo.delete_override(
        session, isolated_project, "gpt-4o",
    )
    assert deleted is True
    rows = await prices_repo.list_overrides(session, isolated_project)
    assert rows == []


async def test_delete_returns_false_for_nonexistent(session, isolated_project):
    deleted = await prices_repo.delete_override(
        session, isolated_project, "model-that-never-was",
    )
    assert deleted is False

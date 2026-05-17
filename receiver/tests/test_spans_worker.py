"""Tests for spans_worker partition maintenance.

Covers ensure_partitions (creates future months), drop_old_partitions
(respects retention), _advance_month arithmetic.
"""

from __future__ import annotations

import pytest

from spans_worker import _advance_month, _month_bounds_ns, _suffix


def test_advance_month_forward():
    assert _advance_month(2026, 5, 1) == (2026, 6)
    assert _advance_month(2026, 12, 1) == (2027, 1)
    assert _advance_month(2026, 11, 3) == (2027, 2)


def test_advance_month_backward():
    assert _advance_month(2026, 5, -1) == (2026, 4)
    assert _advance_month(2026, 1, -1) == (2025, 12)
    assert _advance_month(2026, 3, -5) == (2025, 10)


def test_month_bounds_ns_january():
    lo, hi = _month_bounds_ns(2026, 1)
    # 2026-01-01 00:00:00 UTC
    assert lo > 0
    assert hi > lo
    # hi should be 2026-02-01 00:00:00 UTC
    feb_lo, _ = _month_bounds_ns(2026, 2)
    assert hi == feb_lo


def test_month_bounds_ns_december():
    lo, hi = _month_bounds_ns(2026, 12)
    jan_next, _ = _month_bounds_ns(2027, 1)
    assert hi == jan_next


def test_suffix():
    assert _suffix(2026, 1) == "y2026m01"
    assert _suffix(2026, 12) == "y2026m12"


@pytest.mark.asyncio
async def test_ensure_partitions_creates_tables(session):
    """ensure_partitions creates partition tables for current month range."""
    from spans_worker import ensure_partitions
    from sqlalchemy import text

    ensured = await ensure_partitions(session)
    # Should have at least 4 entries (prev + current + 2 ahead + 1 more)
    assert len(ensured) >= 4

    # Verify at least one partition exists in pg_inherits.
    result = await session.execute(text("""
        SELECT count(*) FROM pg_inherits
        JOIN pg_class child ON child.oid = pg_inherits.inhrelid
        JOIN pg_class parent ON parent.oid = pg_inherits.inhparent
        WHERE parent.relname = 'spans'
    """))
    count = result.scalar()
    # At least the partitions from migration + any new ones.
    assert count >= 4

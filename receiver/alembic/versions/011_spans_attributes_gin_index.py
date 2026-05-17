"""GIN index on spans.attributes for JSONB containment queries.

Adds a GIN index with ``jsonb_path_ops`` operator class to enable
efficient ``attributes @> '{"key": "value"}'::jsonb`` queries. This
is the index strategy that powers span search by arbitrary attribute.

``jsonb_path_ops`` is chosen over the default ``jsonb_ops`` because
it produces a 2-3x smaller index and supports ``@>`` (the only
operator span search uses). Key-existence operators (``?``, ``?|``,
``?&``) are not needed and not indexed under this operator class.

For post-v1 hot-adds to a large production table, the equivalent
``CREATE INDEX CONCURRENTLY`` command is noted in docs/spans.md.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "011"
down_revision: Union[str, Sequence[str], None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_spans_attributes_gin "
        "ON spans USING GIN (attributes jsonb_path_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_spans_attributes_gin")

"""Remove a redundant, incomplete duplicate immutability trigger on
audit.events.

migration 010 already gave audit.events complete UPDATE/DELETE/TRUNCATE
protection via audit.deny_mutation() (events_no_update, events_no_delete,
events_no_truncate). migration 021 added a SECOND, independent trigger on
the same table -- trg_events_immutable, via a different function
(audit.prevent_mutation()) -- that only covers UPDATE and DELETE, not
TRUNCATE. It never added any protection 010 didn't already have; it's
strictly a subset, running on every write for no additional guarantee.

This does NOT touch audit.anchors: migration 010 never protected that
table at all, so trg_anchors_immutable (also from 021, same
audit.prevent_mutation() function) is anchors' ONLY protection --
necessary, not redundant, left alone. audit.prevent_mutation() itself
stays defined for that reason; only the events-table instance of the
trigger using it is dropped here.

Revises: 030
"""
from alembic import op


revision: str = "031"
down_revision: str = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_events_immutable ON audit.events")


def downgrade() -> None:
    op.execute("""
        CREATE TRIGGER trg_events_immutable
        BEFORE UPDATE OR DELETE ON audit.events
        FOR EACH ROW
        EXECUTE FUNCTION audit.prevent_mutation();
    """)

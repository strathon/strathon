"""Give audit.anchors the same TRUNCATE protection audit.events already has.

audit.anchors held the Merkle checkpoints that make the audit log tamper-
evident, but its only immutability guard was trg_anchors_immutable (from 021),
a BEFORE UPDATE OR DELETE ... FOR EACH ROW trigger. Row-level UPDATE/DELETE
triggers do not fire on TRUNCATE, so `TRUNCATE audit.anchors` silently removed
every checkpoint -- the exact gap 031's docstring flagged. events closes this
with a BEFORE TRUNCATE ... FOR EACH STATEMENT trigger plus a REVOKE; mirror both
here so the anchor chain cannot be truncated away.

Revises: 031
"""
from alembic import op


revision: str = "032"
down_revision: str = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Statement-level TRUNCATE trigger (row-level triggers never see TRUNCATE),
    # reusing the same deny function the existing anchors triggers use.
    op.execute(
        """
        CREATE TRIGGER trg_anchors_no_truncate
        BEFORE TRUNCATE ON audit.anchors
        FOR EACH STATEMENT
        EXECUTE FUNCTION audit.prevent_mutation();
        """
    )
    # Defense in depth: remove TRUNCATE (and UPDATE/DELETE) privilege outright,
    # matching how migration 010 hardened audit.events.
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit.anchors FROM strathon;")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit.anchors FROM PUBLIC;")


def downgrade() -> None:
    op.execute("GRANT UPDATE, DELETE, TRUNCATE ON audit.anchors TO strathon;")
    op.execute("DROP TRIGGER IF EXISTS trg_anchors_no_truncate ON audit.anchors;")

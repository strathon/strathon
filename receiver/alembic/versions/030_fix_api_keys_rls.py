"""Fix an api_keys RLS chicken-and-egg deadlock.

migration 021 enabled RLS on every tenant-scoped table including
api_keys, with the policy `project_id = current_setting('app.current_tenant')`.
That's correct for every DATA table (spans, traces, policies, ...) but
wrong for api_keys specifically: an API key lookup is
`SELECT ... FROM api_keys WHERE key_prefix = $1` -- it's how a request
DISCOVERS its project_id, not a query that already knows it. Under a
non-owner DB connection (the only situation where RLS is anything other
than inert -- see 021's own comment about ENABLE vs FORCE) this is an
unsolvable chicken-and-egg: the lookup needs current_tenant set, but
current_tenant can only be set AFTER the lookup resolves project_id.

The credential's own unguessable secret (SHA-256 hash-compared, see
auth.py's module docstring) is already api_keys' real access control --
row-level tenant filtering on the discovery table doesn't add security,
it just deadlocks it.

sessions/users were never in migration 021's RLS_TABLES list, so they
never had this problem: session-based auth carries project_id via the
X-Project-Id header/URL before any DB lookup happens.

This has ZERO effect on today's self-hosted deployment (DATABASE_URL
connects as the table owner, which bypasses RLS regardless of any policy
on any table) and needs no elevated privilege -- ordinary DDL on a table
`strathon` already owns.

Revises: 029
"""
from alembic import op


revision: str = "030"
down_revision: str = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON api_keys")
    op.execute("ALTER TABLE api_keys DISABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON api_keys
        FOR ALL
        USING (
            project_id = current_setting('app.current_tenant', true)::uuid
        )
        WITH CHECK (
            project_id = current_setting('app.current_tenant', true)::uuid
        )
    """)

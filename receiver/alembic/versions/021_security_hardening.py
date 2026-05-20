"""021 — Security hardening: audit immutability, RLS, auth hardening.

Three security layers in one migration:

1. AUDIT IMMUTABILITY: BEFORE UPDATE/DELETE triggers on audit.events and
   audit.anchors that raise exceptions. Even if the app is compromised,
   the attacker cannot rewrite audit history through the DB connection.

2. ROW-LEVEL SECURITY: Enable RLS on all tenant-scoped tables. Policies
   enforce project_id = current_setting('app.current_tenant')::uuid.
   Defense in depth — even if a developer forgets WHERE project_id = on
   a new query, the DB enforces it. Uses SET LOCAL per request.

3. AUTH HARDENING: Add allowed_ips TEXT[] column to api_keys for optional
   IP allowlisting. If set, reject requests from IPs not in the list.
   If null, allow all (backward compatible).

Revision ID: 021
Revises: 020
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


# Tables with project_id that need RLS.
RLS_TABLES = [
    "spans",
    "traces",
    "span_events",
    "policies",
    "policy_matches",
    "policy_versions",
    "api_keys",
    "halt_state",
    "budgets",
    "approvals",
    "webhook_deliveries",
    "webhook_signing_keys",
    "project_settings",
    "model_price_overrides",
    "intervention_log",
]

# Audit schema tables (schema-qualified).
AUDIT_RLS_TABLES = [
    ("audit", "events"),
    ("audit", "streams"),
]


def upgrade() -> None:
    # ---- 1. Audit immutability triggers ----

    # Trigger function: raise exception on UPDATE or DELETE.
    op.execute("""
        CREATE OR REPLACE FUNCTION audit.prevent_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'audit.% is immutable: % operations are prohibited',
                TG_TABLE_NAME, TG_OP
            USING ERRCODE = 'insufficient_privilege';
        END;
        $$ LANGUAGE plpgsql;
    """)

    # Trigger on audit.events.
    op.execute("""
        CREATE TRIGGER trg_events_immutable
        BEFORE UPDATE OR DELETE ON audit.events
        FOR EACH ROW
        EXECUTE FUNCTION audit.prevent_mutation();
    """)

    # Trigger on audit.anchors.
    op.execute("""
        CREATE TRIGGER trg_anchors_immutable
        BEFORE UPDATE OR DELETE ON audit.anchors
        FOR EACH ROW
        EXECUTE FUNCTION audit.prevent_mutation();
    """)

    # ---- 2. Row-Level Security ----

    # Public schema tables.
    # ENABLE but not FORCE: the table owner bypasses RLS by default.
    # For production multi-role setups, operators should FORCE RLS on
    # the app role or use a non-owner DB user for the application.
    # The policies are pre-configured and enforce automatically for
    # any non-owner role.
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
            FOR ALL
            USING (
                project_id = current_setting('app.current_tenant', true)::uuid
            )
            WITH CHECK (
                project_id = current_setting('app.current_tenant', true)::uuid
            )
        """)

    # Audit schema tables.
    for schema, table in AUDIT_RLS_TABLES:
        fqn = f"{schema}.{table}"
        op.execute(f"ALTER TABLE {fqn} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {fqn}
            FOR ALL
            USING (
                project_id = current_setting('app.current_tenant', true)::uuid
            )
            WITH CHECK (
                project_id = current_setting('app.current_tenant', true)::uuid
            )
        """)

    # ---- 3. Auth hardening: IP allowlist ----

    op.add_column(
        "api_keys",
        sa.Column(
            "allowed_ips",
            ARRAY(sa.Text),
            nullable=True,
            comment=(
                "Optional IP allowlist. If set, requests from IPs not "
                "in this list are rejected. Null means allow all."
            ),
        ),
    )


def downgrade() -> None:
    # Auth hardening.
    op.drop_column("api_keys", "allowed_ips")

    # RLS.
    for schema, table in AUDIT_RLS_TABLES:
        fqn = f"{schema}.{table}"
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {fqn}")
        op.execute(f"ALTER TABLE {fqn} DISABLE ROW LEVEL SECURITY")

    for table in reversed(RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    # Audit immutability.
    op.execute("DROP TRIGGER IF EXISTS trg_anchors_immutable ON audit.anchors")
    op.execute("DROP TRIGGER IF EXISTS trg_events_immutable ON audit.events")
    op.execute("DROP FUNCTION IF EXISTS audit.prevent_mutation()")

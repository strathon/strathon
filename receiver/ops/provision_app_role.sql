-- provision_app_role.sql
--
-- Creates a non-owner Postgres role for a multi-tenant / cloud deployment
-- to connect as, so the RLS policies from migration 021 (and the
-- api_keys fix in migration 030) actually apply. NOT part of the
-- automatic `alembic upgrade head` chain -- run manually, once, only
-- when standing up a multi-tenant deployment.
--
-- Why this isn't a migration: CREATE ROLE requires CREATEROLE or
-- superuser -- a cluster-wide privilege, not the table-level DDL
-- privilege the app's normal DB role has via table ownership. The
-- vanilla docker-compose deployment's POSTGRES_USER happens to be a
-- full superuser (the official postgres image bootstraps it that way),
-- but plenty of real deployments point DATABASE_URL at managed Postgres
-- (RDS, Cloud SQL, Supabase, Neon, ...) where the app's role deliberately
-- does NOT have CREATEROLE, by the platform's own design. Baking this into
-- `alembic upgrade head` would hard-fail those deployments on every future
-- upgrade for a role they'll never use. Self-host is single-tenant by
-- definition (one deployment, one project, nothing to isolate) and never
-- needs this at all.
--
-- Prerequisites:
--   * migration 030 has been applied (fixes the api_keys RLS deadlock;
--     without it, authenticating as this role can never succeed --
--     see that migration's docstring for why)
--   * you're connecting as a role with CREATEROLE, or as the cluster
--     superuser (e.g. the docker-compose default `strathon` role, or an
--     RDS/Cloud SQL admin account)
--
-- Usage:
--   psql "$DATABASE_URL" -f ops/provision_app_role.sql
--   ALTER ROLE strathon_app WITH PASSWORD '<a real generated secret>';
--   -- then point the RECEIVER's DATABASE_URL (not psql's) at strathon_app
--
-- What this does NOT do: switch anything over. After running this, the
-- receiver keeps connecting as whatever DATABASE_URL already said (almost
-- always the table owner) until an operator deliberately changes it.
-- Before ever pointing production request traffic at strathon_app, run a
-- full functional pass first: every endpoint category, every background
-- task (retention, budget_monitor, incident_detector, key_reaper,
-- approval_reaper, webhooks/actor, spans_worker partition maintenance,
-- audit/worker anchor sealing) -- background tasks in particular were
-- written assuming an owner-equivalent (RLS-bypassing) connection and
-- most loop across ALL projects in one pass; each such loop needs an
-- explicit `SELECT set_config('app.current_tenant', <project_id>, true)`
-- per iteration if it's ever run against strathon_app instead of the
-- owner role. That per-task audit has not been done as of this writing
-- (2026-07) -- do it before cutover, not after something silently stops
-- working in production.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'strathon_app') THEN
        CREATE ROLE strathon_app WITH LOGIN PASSWORD 'CHANGE_ME_BEFORE_PRODUCTION';
    END IF;
END
$$;

-- Schema-level grants, not per-table: RLS does the actual row-level
-- restriction (already in place from migration 021). GRANT here only
-- needs to establish "this role may touch these tables at all" -- doing
-- it at the schema level means a table a future migration adds is
-- covered automatically, with no separate grant statement to remember
-- and no risk of a silently-forgotten table.
GRANT USAGE ON SCHEMA public TO strathon_app;
GRANT USAGE ON SCHEMA audit TO strathon_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO strathon_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO strathon_app;

-- audit.events / audit.anchors are append-only by design (the
-- immutability triggers from migration 021 block UPDATE/DELETE at the
-- trigger level regardless of GRANT) -- INSERT + SELECT only, matching
-- the grant migration 010 gives the owner-equivalent role explicitly.
GRANT SELECT, INSERT ON audit.events TO strathon_app;
GRANT SELECT, INSERT ON audit.anchors TO strathon_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON audit.streams TO strathon_app;
GRANT USAGE, SELECT ON SEQUENCE audit.events_sequence_no_seq TO strathon_app;

-- Cover tables/sequences created by migrations that haven't run yet, as
-- long as they're created by the `strathon` owner role (true for every
-- migration in this repo -- they all run as whatever DATABASE_URL says,
-- which is the owner).
ALTER DEFAULT PRIVILEGES FOR ROLE strathon IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO strathon_app;
ALTER DEFAULT PRIVILEGES FOR ROLE strathon IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO strathon_app;
ALTER DEFAULT PRIVILEGES FOR ROLE strathon IN SCHEMA audit
    GRANT SELECT, INSERT ON TABLES TO strathon_app;
ALTER DEFAULT PRIVILEGES FOR ROLE strathon IN SCHEMA audit
    GRANT USAGE, SELECT ON SEQUENCES TO strathon_app;

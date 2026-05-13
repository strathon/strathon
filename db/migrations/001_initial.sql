-- Strathon Database Schema
-- Migration 001: Initial schema
-- Target: Postgres 17+
-- 
-- Design follows the OTel GenAI semconv research output:
--   - gen_ai.* attributes for OpenTelemetry-standard fields (denormalized as columns)
--   - strathon.agent.* attributes for agent-specific topology, budget, intervention
--   - JSONB attributes column for everything else
--   - Native parent_span_id graph for topology; span_links for non-tree edges
--   - Span events for intervention moments; halt_state table for cross-restart WAL

-- ============================================================
-- EXTENSIONS
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- CORE ENTITIES
-- ============================================================

CREATE TABLE projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX idx_projects_slug ON projects(slug) WHERE deleted_at IS NULL;

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    github_id BIGINT UNIQUE NOT NULL,
    github_username TEXT NOT NULL,
    email TEXT,
    avatar_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMPTZ
);

CREATE INDEX idx_users_github_id ON users(github_id);

CREATE TABLE project_members (
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, user_id)
);

CREATE INDEX idx_project_members_user ON project_members(user_id);

CREATE TABLE api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);

CREATE INDEX idx_api_keys_project ON api_keys(project_id) WHERE revoked_at IS NULL;
CREATE INDEX idx_api_keys_prefix ON api_keys(key_prefix) WHERE revoked_at IS NULL;

CREATE TABLE sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ip_address INET,
    user_agent TEXT
);

CREATE INDEX idx_sessions_user ON sessions(user_id) WHERE expires_at > NOW();
CREATE INDEX idx_sessions_token ON sessions(token_hash) WHERE expires_at > NOW();

-- ============================================================
-- TRACE DATA
-- ============================================================

CREATE TABLE traces (
    -- 16-byte OTel trace_id
    id BYTEA PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    
    start_time_unix_nano BIGINT NOT NULL,
    end_time_unix_nano BIGINT,  -- null while trace is open
    -- 8-byte OTel span_id of root span
    root_span_id BYTEA,
    
    -- Denormalized for fast queries and dashboard list views
    agent_name TEXT,
    workflow_name TEXT,
    conversation_id TEXT,
    git_commit_sha TEXT,
    
    -- Cost rollup (computed by receiver on trace close)
    total_cost_usd NUMERIC(12, 6) DEFAULT 0,
    total_input_tokens INT DEFAULT 0,
    total_output_tokens INT DEFAULT 0,
    span_count INT DEFAULT 0,
    
    -- Intervention state at trace level
    intervention_state TEXT DEFAULT 'running'
        CHECK (intervention_state IN ('running', 'paused', 'halted', 'completed')),
    halt_reason TEXT,
    
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_traces_project_time ON traces(project_id, start_time_unix_nano DESC);
CREATE INDEX idx_traces_project_agent ON traces(project_id, agent_name) 
    WHERE agent_name IS NOT NULL;
CREATE INDEX idx_traces_git_commit ON traces(project_id, git_commit_sha) 
    WHERE git_commit_sha IS NOT NULL;
CREATE INDEX idx_traces_intervention ON traces(project_id, intervention_state) 
    WHERE intervention_state != 'completed';

CREATE TABLE spans (
    trace_id BYTEA NOT NULL,
    -- 8-byte OTel span_id
    span_id BYTEA NOT NULL,
    -- 8-byte; null for root span
    parent_span_id BYTEA,
    project_id UUID NOT NULL,
    
    -- OTel core fields
    name TEXT NOT NULL,
    kind TEXT NOT NULL 
        CHECK (kind IN ('CLIENT', 'INTERNAL', 'SERVER', 'PRODUCER', 'CONSUMER', 'UNSPECIFIED')),
    start_time_unix_nano BIGINT NOT NULL,
    end_time_unix_nano BIGINT,  -- null while span is open (streaming support)
    
    status_code TEXT CHECK (status_code IN ('OK', 'ERROR', 'UNSET')),
    status_message TEXT,
    
    -- gen_ai.* attributes (denormalized, indexed)
    operation_name TEXT,         -- gen_ai.operation.name
    provider_name TEXT,          -- gen_ai.provider.name
    request_model TEXT,          -- gen_ai.request.model
    response_model TEXT,         -- gen_ai.response.model
    agent_name TEXT,             -- gen_ai.agent.name
    agent_id TEXT,               -- gen_ai.agent.id
    tool_name TEXT,              -- gen_ai.tool.name
    workflow_name TEXT,          -- gen_ai.workflow.name
    conversation_id TEXT,        -- gen_ai.conversation.id
    
    input_tokens INT,            -- gen_ai.usage.input_tokens
    output_tokens INT,           -- gen_ai.usage.output_tokens
    reasoning_tokens INT,        -- gen_ai.usage.reasoning.output_tokens
    cache_read_tokens INT,       -- gen_ai.usage.cache_read.input_tokens
    cache_creation_tokens INT,   -- gen_ai.usage.cache_creation.input_tokens
    
    -- strathon.agent.* attributes (sampling-relevant, indexed)
    agent_depth INT,                          -- strathon.agent.depth
    spawn_parent_agent_id TEXT,               -- strathon.agent.spawn.parent_agent_id
    spawn_reason TEXT,                        -- strathon.agent.spawn.reason
    cost_usd NUMERIC(12, 6),                  -- strathon.agent.cost.usd
    cost_cumulative_usd NUMERIC(12, 6),       -- strathon.agent.cost.cumulative_usd
    tokens_subtree_input INT,                 -- strathon.agent.tokens.input_subtree
    tokens_subtree_output INT,                -- strathon.agent.tokens.output_subtree
    cost_subtree_usd NUMERIC(12, 6),          -- strathon.agent.cost.subtree_usd
    
    intervention_state TEXT,                  -- strathon.agent.intervention.state
    halt_reason TEXT,                         -- strathon.agent.halt.reason
    
    -- Everything else (input.messages, output.messages, tool.call.arguments, etc.)
    attributes JSONB NOT NULL DEFAULT '{}',
    
    FOREIGN KEY (trace_id) REFERENCES traces(id) ON DELETE CASCADE,
    PRIMARY KEY (trace_id, span_id)
);

CREATE INDEX idx_spans_project_time ON spans(project_id, start_time_unix_nano DESC);
CREATE INDEX idx_spans_trace_time ON spans(trace_id, start_time_unix_nano);
CREATE INDEX idx_spans_parent ON spans(trace_id, parent_span_id);
CREATE INDEX idx_spans_agent ON spans(project_id, agent_name, start_time_unix_nano DESC) 
    WHERE agent_name IS NOT NULL;
CREATE INDEX idx_spans_tool ON spans(project_id, tool_name, start_time_unix_nano DESC) 
    WHERE tool_name IS NOT NULL;
CREATE INDEX idx_spans_operation ON spans(project_id, operation_name, start_time_unix_nano DESC) 
    WHERE operation_name IS NOT NULL;
CREATE INDEX idx_spans_intervention ON spans(project_id, intervention_state) 
    WHERE intervention_state IS NOT NULL AND intervention_state != 'running';

CREATE TABLE span_events (
    -- Span events (used for intervention moments per OTel pattern)
    id BIGSERIAL PRIMARY KEY,
    trace_id BYTEA NOT NULL,
    span_id BYTEA NOT NULL,
    project_id UUID NOT NULL,
    name TEXT NOT NULL,
    time_unix_nano BIGINT NOT NULL,
    attributes JSONB NOT NULL DEFAULT '{}',
    FOREIGN KEY (trace_id, span_id) REFERENCES spans(trace_id, span_id) ON DELETE CASCADE
);

CREATE INDEX idx_span_events_trace ON span_events(trace_id, time_unix_nano);
CREATE INDEX idx_span_events_intervention ON span_events(project_id, time_unix_nano DESC) 
    WHERE name LIKE 'strathon.agent.intervention%';

CREATE TABLE span_links (
    -- Non-tree edges: tool->llm provenance, retry-from-checkpoint, cross-agent handoffs
    id BIGSERIAL PRIMARY KEY,
    trace_id BYTEA NOT NULL,
    span_id BYTEA NOT NULL,
    linked_trace_id BYTEA NOT NULL,
    linked_span_id BYTEA NOT NULL,
    attributes JSONB NOT NULL DEFAULT '{}',
    FOREIGN KEY (trace_id, span_id) REFERENCES spans(trace_id, span_id) ON DELETE CASCADE
);

CREATE INDEX idx_span_links_span ON span_links(trace_id, span_id);

-- ============================================================
-- RUNTIME INTERVENTION
-- The architectural moat: persistent halt state across process restarts,
-- cross-process budget rollup via parent_budget_id hierarchy.
-- ============================================================

CREATE TABLE budgets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    
    max_spend_usd NUMERIC(12, 6) NOT NULL,
    spent_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
    soft_limit_ratio NUMERIC(4, 3) DEFAULT 0.9,
    
    -- Cross-process rollup: child budgets count against parent ceiling
    parent_budget_id UUID REFERENCES budgets(id) ON DELETE SET NULL,
    
    -- Loop detection thresholds
    max_repeated_calls INT,
    loop_window_seconds NUMERIC(8, 2),
    
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);

CREATE INDEX idx_budgets_project ON budgets(project_id) WHERE is_active = true;
CREATE INDEX idx_budgets_parent ON budgets(parent_budget_id) WHERE parent_budget_id IS NOT NULL;

CREATE TABLE halt_state (
    -- WRITE-AHEAD LOG for cross-restart halt persistence.
    -- The architectural moat. Append-only. SDK reads on init to restore halt state
    -- across process restarts. When kill -9 happens, this survives.
    id BIGSERIAL PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    
    -- Scope of the halt (at least one must be set)
    trace_id BYTEA,
    agent_id TEXT,
    budget_id UUID REFERENCES budgets(id) ON DELETE CASCADE,
    
    state TEXT NOT NULL 
        CHECK (state IN ('paused', 'halted', 'resumed', 'cleared')),
    reason TEXT NOT NULL,
    actor TEXT NOT NULL 
        CHECK (actor IN ('budget_monitor', 'loop_detector', 'user', 'policy_engine', 'system')),
    
    set_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cleared_at TIMESTAMPTZ,
    cleared_by_user_id UUID REFERENCES users(id),
    
    metadata JSONB DEFAULT '{}',
    
    CONSTRAINT chk_halt_scope CHECK (
        trace_id IS NOT NULL OR agent_id IS NOT NULL OR budget_id IS NOT NULL
    )
);

CREATE INDEX idx_halt_state_trace ON halt_state(project_id, trace_id, set_at DESC) 
    WHERE trace_id IS NOT NULL;
CREATE INDEX idx_halt_state_agent ON halt_state(project_id, agent_id, set_at DESC) 
    WHERE agent_id IS NOT NULL;
CREATE INDEX idx_halt_state_active ON halt_state(project_id, state, set_at DESC) 
    WHERE cleared_at IS NULL AND state IN ('paused', 'halted');

CREATE TABLE intervention_log (
    -- Audit trail: every intervention decision made by the SDK
    id BIGSERIAL PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    trace_id BYTEA NOT NULL,
    span_id BYTEA,
    
    decision TEXT NOT NULL 
        CHECK (decision IN ('allowed', 'blocked', 'paused', 'resumed')),
    reason TEXT,
    
    estimated_cost_usd NUMERIC(12, 6),
    budget_remaining_usd NUMERIC(12, 6),
    loop_count INT,
    
    decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX idx_intervention_log_trace ON intervention_log(trace_id, decided_at DESC);
CREATE INDEX idx_intervention_log_project_time ON intervention_log(project_id, decided_at DESC);

-- ============================================================
-- GITHUB INTEGRATION
-- ============================================================

CREATE TABLE github_integrations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    repo_full_name TEXT NOT NULL,
    installation_id BIGINT,
    webhook_secret TEXT NOT NULL,
    
    created_by_user_id UUID NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_event_at TIMESTAMPTZ,
    
    UNIQUE (project_id, repo_full_name)
);

CREATE INDEX idx_github_integrations_project ON github_integrations(project_id);

CREATE TABLE git_commits (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    integration_id UUID REFERENCES github_integrations(id) ON DELETE SET NULL,
    
    commit_sha TEXT NOT NULL,
    repo_full_name TEXT NOT NULL,
    commit_message TEXT,
    author_name TEXT,
    author_email TEXT,
    committed_at TIMESTAMPTZ,
    
    branch TEXT,
    pr_number INT,
    
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, commit_sha)
);

CREATE INDEX idx_git_commits_sha ON git_commits(project_id, commit_sha);
CREATE INDEX idx_git_commits_committed_at ON git_commits(project_id, committed_at DESC) 
    WHERE committed_at IS NOT NULL;

-- ============================================================
-- PROJECT SETTINGS
-- ============================================================

CREATE TABLE project_settings (
    project_id UUID PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    
    -- PII redaction default-on per Strathon positioning
    pii_redaction_enabled BOOLEAN NOT NULL DEFAULT true,
    pii_redaction_patterns JSONB DEFAULT '[]',
    
    -- Content capture (off by default per OTel GenAI spec)
    content_capture_enabled BOOLEAN NOT NULL DEFAULT false,
    
    trace_retention_days INT NOT NULL DEFAULT 30,
    
    intervention_default_action TEXT DEFAULT 'allow'
        CHECK (intervention_default_action IN ('allow', 'block')),
    
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by_user_id UUID REFERENCES users(id)
);

-- ============================================================
-- TRIGGERS for updated_at
-- ============================================================

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_projects_updated_at 
    BEFORE UPDATE ON projects 
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_budgets_updated_at 
    BEFORE UPDATE ON budgets 
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_project_settings_updated_at 
    BEFORE UPDATE ON project_settings 
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- SEED DATA (default project for fresh installs)
-- ============================================================

INSERT INTO projects (id, name, slug) 
VALUES ('00000000-0000-0000-0000-000000000001', 'Default', 'default');

INSERT INTO project_settings (project_id) 
VALUES ('00000000-0000-0000-0000-000000000001');

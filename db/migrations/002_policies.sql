-- ============================================================
-- 002: Runtime Intervention Policies
-- ============================================================
-- Adds the `policies` table for runtime intervention rules.
-- Each policy has a match expression (JSON tree) and an action:
--   'log'   - mark matching spans with strathon.policy.* attributes
--   'alert' - fire webhook async when match occurs (server-side)
--   'block' - SDK raises StrathonPolicyBlocked before action executes
--   'steer' - SDK returns a corrective string in place of tool output

CREATE TABLE policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,

    -- Match expression as a CEL (Common Expression Language) string.
    -- CEL is the same expression language Kubernetes, Envoy, and gcloud IAM
    -- use for policy rules. It is non-Turing-complete, side-effect free,
    -- and guaranteed to terminate.
    --
    -- Example:
    --   attrs["gen_ai.tool.name"] == "send_email" &&
    --   attrs["strathon.tool.args"].contains("@competitor.com")
    --
    -- Context shape: {"name": <span name>, "attrs": <flat attributes map>}.
    match_expression TEXT NOT NULL,

    -- One of: 'log', 'alert', 'block', 'steer'
    action TEXT NOT NULL CHECK (action IN ('log', 'alert', 'block', 'steer')),

    -- Action-specific config:
    --   alert: {"webhook_url": "https://..."}
    --   steer: {"replacement": "BLOCKED: ..."}
    --   block: optional {"message": "..."}
    --   log: typically empty
    action_config JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Which span types this policy applies to. Empty array = all spans.
    -- Example: ["tool", "llm"] limits to tool calls and LLM calls.
    applies_to TEXT[] NOT NULL DEFAULT '{}',

    enabled BOOLEAN NOT NULL DEFAULT TRUE,

    -- Priority: higher numbers evaluate first. Lets users say
    -- "the block rule always wins over the log rule".
    priority INT NOT NULL DEFAULT 0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_policies_project_enabled 
    ON policies(project_id, enabled) 
    WHERE enabled = TRUE;

CREATE INDEX idx_policies_priority 
    ON policies(project_id, priority DESC) 
    WHERE enabled = TRUE;

CREATE TRIGGER trg_policies_updated_at 
    BEFORE UPDATE ON policies 
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Audit log of every policy match (separate from spans table for
-- queryability and to avoid blowing up the main spans index).
CREATE TABLE policy_matches (
    id BIGSERIAL PRIMARY KEY,
    policy_id UUID NOT NULL REFERENCES policies(id) ON DELETE CASCADE,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    trace_id BYTEA NOT NULL,
    span_id BYTEA NOT NULL,
    action TEXT NOT NULL,
    action_outcome TEXT,                 -- 'logged', 'alerted', 'blocked', 'steered', 'webhook_failed', etc.
    matched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_policy_matches_policy ON policy_matches(policy_id, matched_at DESC);
CREATE INDEX idx_policy_matches_project ON policy_matches(project_id, matched_at DESC);
CREATE INDEX idx_policy_matches_trace ON policy_matches(trace_id);

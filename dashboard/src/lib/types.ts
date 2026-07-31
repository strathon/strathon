/**
 * Types for the payloads the dashboard's BFF routes (src/app/api/**) return.
 *
 * Those routes map raw receiver responses through the transforms in
 * ./transforms.ts. The receiver's list payloads carry optional, endpoint-
 * specific fields and the dashboard reads them defensively (optional chaining
 * with fallbacks), so fields here are optional. Each interface is the contract
 * for one endpoint's rows -- a single source of truth the pages import rather
 * than re-typing inline. Some fields appear in both camelCase and snake_case
 * because a page may render either the transformed or the raw shape.
 */

/** A row from GET /api/agents (agent inventory). */
export interface AgentRow {
  id: string;
  name: string;
  description?: string;
  owner?: string;
  risk: number;
  calls: number;
  spend: number;
  models: number;
  policies: number;
  live?: boolean;
  lastActive?: string | null;
  last_active?: string | null;
}

/** A row from GET /api/approvals (pending human-approval requests). */
export interface ApprovalRow {
  id: string;
  agent: string;
  tool: string;
  policy: string;
  params?: unknown;
  expiresIn: number;
}

/** A row from GET /api/audit (audit event log). */
export interface AuditRow {
  id: string;
  action: string;
  category: string;
  actor: string;
  actor_type?: string;
  target?: string;
  resource?: string;
  outcome: string;
  status?: string;
  ip?: string;
  timestamp?: string;
  ts: number;
}

/** A row from GET /api/policies (policy list). */
export interface PolicyRow {
  id: string;
  name: string;
  description?: string;
  action: string;
  status: string;
  priority: number;
  hits7d: number[];
  lastModified?: string;
  last_modified?: string;
}

/** GET /api/policies/:id (single policy, with version history). */
export interface PolicyDetail {
  name?: string;
  description?: string;
  action?: string;
  status?: string;
  priority?: number;
  hits7d?: number[];
  cel?: string;
  expression?: string;
  versions?: PolicyVersion[];
}

export interface PolicyVersion {
  version?: number;
  v?: number;
  created_at?: string;
  when?: string;
  by?: string;
  author?: string;
  note?: string;
}

/** A row from GET /api/members (workspace members). */
export interface MemberRow {
  id: string;
  name: string;
  display_name?: string;
  email: string;
  role: string;
  joined?: string;
  joined_at?: string;
  last_active?: string | null;
}

/** A row from GET /api/traces (trace list). */
export interface TraceRow {
  id: string;
  shortId?: string;
  agent: string;
  operation: string;
  spans?: number;
  span_count?: number;
  durationMs?: number;
  duration_ms?: number;
  status: string;
  started?: string;
  start_time?: string;
}

/** The GET /api/budgets object (summary + rule rows). */
export interface BudgetData {
  rules?: BudgetRow[];
  spend_mtd?: number;
  active_rules?: number;
}

/** A budget rule row from GET /api/budgets. */
export interface BudgetRow {
  id: string;
  name: string;
  kind: string;
  scope?: string;
  period?: string;
  threshold: number;
  status: string;
}

/** A framework block from GET /api/compliance. */
export interface ComplianceFramework {
  id: string;
  name: string;
  description?: string;
  coverage: number;
  controls?: number;
  recs?: number;
}

export interface ComplianceControl {
  id?: string;
  name?: string;
  status?: string;
}

/** A recommendation card from GET /api/compliance. */
export interface ComplianceRecommendation {
  framework?: string;
  title?: string;
  detail?: string;
  cta?: string;
}

/** A command-palette / navigation entry used by the shell. */
export interface CommandItem {
  id?: string;
  label: string;
  icon?: string;
  section?: string;
  to?: string;
  action?: string;
  kbd?: string;
  badge?: string;
}

/** GET /api/traces/:id (single trace with its span waterfall). */
export interface TraceDetail {
  id?: string;
  shortId?: string;
  agent?: string;
  operation?: string;
  status?: string;
  started?: string;
  spans?: number;
  waterfall_spans?: unknown[];
}

/** A row from GET /api/api-keys. */
export interface ApiKeyRow {
  id: string;
  name: string;
  prefix?: string;
  key_prefix?: string;
  created?: string;
  created_at?: string;
  last_used?: string | null;
  last_used_at?: string | null;
}

/** A notification channel from GET /api/notifications. */
export interface NotificationChannel {
  id: string;
  name: string;
  channel_type: string;
  events?: string[];
  enabled: boolean;
}

/** Result of a policy simulation (POST /api/policies/:id/simulate). */
export interface SimResult {
  evaluated?: number;
  new_blocks?: number;
  would_flag?: number;
  reason?: string;
  examples?: SimExample[];
}

export interface SimExample {
  agent?: string;
  tool?: string;
  traceId?: string;
  trace_id?: string;
  reason?: string;
}

/** A pending workspace transfer/invite (GET /api/members pending list). */
export interface TransferRow {
  id: string;
  email: string;
  display_name?: string;
  project_name?: string;
  role: string;
  invited_at?: string;
}

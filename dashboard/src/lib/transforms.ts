/**
 * Response transforms for the dashboard BFF.
 *
 * The receiver speaks resource-specific shapes ({ policies: [...] },
 * { agents: [...] }, budget rule lists, etc.). The dashboard pages read a
 * normalized shape ({ data: [...] }) with their own field names. These
 * functions map one to the other using only fields the receiver actually
 * provides — no fabricated values. Where the receiver has no backing data
 * for a widget, the field is simply absent and the page degrades cleanly.
 */

type Obj = Record<string, unknown>;
const asArray = (v: unknown): Obj[] => (Array.isArray(v) ? (v as Obj[]) : []);
const num = (v: unknown, d = 0): number => {
  const n = typeof v === "string" ? parseFloat(v) : typeof v === "number" ? v : NaN;
  return Number.isFinite(n) ? n : d;
};

// Classify a span into a kind for colouring. A single-agent trace has one
// "service", so colouring by service is monochrome; kind (llm/tool/agent/
// retrieval) is meaningful per-span. Shared by mapTraceTree (tree nodes) and
// mapSpans (flat rows) so the two never drift.
//
// Patterns from the real framework instrumentations (sdk/src/strathon/
// instrumentation/*.py):
//   LangGraph/LangChain : langgraph.chain.{name}, langgraph.llm,
//                         langgraph.tool.{name}
//   CrewAI     : crewai.crew.{name}, crewai.task.{name}, crewai.agent.{role},
//                crewai.llm
//   OpenAI     : openai.chat.{model}
//   Anthropic  : anthropic.messages.{model}
//   OAI Agents : agents.workflow.{name}, agents.agent, agents.generation,
//                agents.response, agents.tool, agents.handoff, agents.turn
//   AutoGen    : autogen.agent.{name}, autogen.team.{name}, autogen.tool.{name}
//   Claude SDK : claude_agent.query, claude_agent.client.{name},
//                claude_agent.tool.{name}
//   Google ADK : google_adk.model.{model}, google_adk.tool.{name}
//   Pydantic AI: pydantic_ai.tool.{name}, pydantic_ai chat spans
//
// Most spans also carry request_model/provider_name/agent_name/tool_name from
// the receiver's gen_ai.* mapping, which the checks below use as the primary
// signal; the name patterns are the fallback when those fields are absent.
// Order matters: tool checks first (so "agent.tool.send_email" -> tool, not
// agent), then retrieval, then llm, then agent/chain.
function spanKind(o: Obj): "agent" | "llm" | "tool" | "retrieval" | "other" {
  const name = String(o.name || o.operation_name || "").toLowerCase();
  if (o.tool_name || /(^|\.)tool(\.|s\.)|\.tool$/.test(name)) return "tool";
  if (/(retriev|\.vector|\.embed|knowledge)/.test(name)) return "retrieval";
  if (
    o.request_model ||
    o.provider_name ||
    /(^|\.)llm(\.|$)|\.chat(\.|$)|\.messages(\.|$)|\.completion|\.generation(\.|$)|\.response(s)?(\.|$)|\.model(\.|$)/.test(name)
  ) return "llm";
  if (
    /\.chain(\.|$)|(^|\.)agent(\.|$)|\.crew(\.|$)|\.task(\.|$)|\.team(\.|$)|\.workflow(\.|$)|\.handoff(\.|$)|\.turn(\.|$)|\.query(\.|$)|\.client(\.|$)|\.session|\.respond|\.plan/.test(name) ||
    (o.agent_name && !o.tool_name)
  ) return "agent";
  return "other";
}


/** Policies: { policies: [...] } -> { data: [...], total }. */
export function mapPolicies(body: unknown): unknown {
  const b = (body || {}) as Obj;
  const rows = asArray(b.policies ?? b.data);
  const data = rows.map((p) => {
    const enabled = p.enabled !== false;
    const shadow = p.shadow === true;
    const status = !enabled ? "disabled" : shadow ? "shadow" : "enabled";
    const hits = num(p.match_count);
    return {
      id: p.id,
      name: p.name,
      description: p.description ?? "",
      action: p.action,
      priority: num(p.priority),
      status,
      cel: p.match_expression,
      // The list endpoint exposes a lifetime match count, not a daily
      // series, so the sparkline shows the real total as a single point.
      hits7d: [hits],
      last_modified: p.updated_at ?? p.created_at ?? null,
    };
  });
  return { data, total: data.length, intervention_default_action: b.intervention_default_action };
}

/** Approvals: { approvals: [...] } -> { data: [...] }. */
export function mapApprovals(body: unknown): unknown {
  const rows = asArray((body as Obj)?.approvals ?? (body as Obj)?.data);
  const now = Date.now();
  const data = rows.map((a) => {
    const expiresAt = a.expires_at ? Date.parse(String(a.expires_at)) : NaN;
    const expiresIn = Number.isFinite(expiresAt) ? Math.max(0, Math.round((expiresAt - now) / 1000)) : 0;
    return {
      id: a.id,
      agent: a.agent_name ?? a.agent ?? "unknown agent",
      tool: a.tool_name ?? a.tool ?? "",
      policy: a.policy_name ?? a.policy ?? "",
      params: a.tool_arguments ?? a.params ?? null,
      status: a.status,
      expiresIn,
    };
  });
  return { data };
}

/** Agents: { agents: [...] } -> { data: [...] }. */
const RISK_NUM: Record<string, number> = { critical: 90, high: 75, medium: 50, low: 20, minimal: 8 };
export function mapAgents(body: unknown): unknown {
  const b = (body || {}) as Obj;
  const rows = asArray(b.agents ?? b.data);
  const now = Date.now();
  const data = rows.map((a) => {
    const lastActive = a.last_active ? Date.parse(String(a.last_active)) : NaN;
    const live = Number.isFinite(lastActive) && now - lastActive < 5 * 60 * 1000;
    const riskRaw = a.risk_score;
    const risk = typeof riskRaw === "number" ? riskRaw : RISK_NUM[String(riskRaw).toLowerCase()] ?? 0;
    return {
      id: a.agent_name ?? a.id,
      name: a.agent_name ?? a.name,
      risk,
      risk_factors: a.risk_factors ?? [],
      calls: num(a.total_tool_calls),
      models: Array.isArray(a.models_used) ? a.models_used.length : num(a.models),
      spend: num(a.total_cost_usd),
      policies: num(a.policies_covering),
      live,
      last_active: a.last_active ?? null,
    };
  });
  return { data };
}

/**
 * Budgets: receiver returns a list of budget rules. The page wants a
 * summary object. We surface the real rules and the real spend total;
 * forecast/daily series are left absent (the receiver exposes those via a
 * separate forecast endpoint, wired in the budgets route) so nothing is
 * fabricated here.
 */
export function mapBudgets(body: unknown): unknown {
  const rows = asArray((body as Obj)?.budgets ?? (body as Obj)?.data);
  const rules = rows.map((r) => ({
    id: r.id,
    name: r.name,
    description: r.description ?? "",
    scope: r.scope,
    threshold: num(r.max_spend_usd),
    max_spend_usd: num(r.max_spend_usd),
    spent_usd: num(r.spent_usd),
    status: r.is_active === false ? "disabled" : "enabled",
    period: r.budget_duration ?? r.duration ?? "monthly",
    duration: r.budget_duration ?? "monthly",
    kind: r.max_repeated_calls != null ? "iteration" : "cost",
    reset_at: r.budget_reset_at ?? null,
  }));
  const spendMtd = rules.reduce((a, r) => a + r.spent_usd, 0);
  const activeRules = rules.filter((r) => r.status === "enabled").length;
  return { data: { rules, spend_mtd: spendMtd, active_rules: activeRules } };
}

/**
 * Compliance: the receiver has no framework-coverage endpoint, so the
 * coverage widget has no real backing. Return an empty framework list so
 * the page shows its honest "no frameworks configured" empty state rather
 * than fabricated percentages. Compliance reporting is via the export/SARIF
 * endpoints, surfaced elsewhere.
 */
export function mapCompliance(): unknown {
  return { data: [], frameworks: [] };
}

/** API keys: { api_keys: [...] } -> { data: [...] }. Field names already
 *  match what the settings table reads (key_prefix, created_at, last_used_at). */
export function mapApiKeys(body: unknown): unknown {
  const rows = asArray((body as Obj)?.api_keys ?? (body as Obj)?.data);
  return { data: rows };
}

/**
 * Audit events: receiver returns { data: [AuditEventRead] } from
 * /v1/audit/events with nested actor/resource objects and occurred_at.
 * The audit page reads flat fields (ts, actor string, category). Map them.
 */
export function mapAudit(body: unknown): unknown {
  const rows = asArray((body as Obj)?.data ?? (body as Obj)?.events);
  const data = rows.map((e) => {
    const actor = (e.actor || {}) as Obj;
    const resource = (e.resource || {}) as Obj;
    return {
      id: e.id,
      ts: e.occurred_at ?? e.ingested_at ?? null,
      timestamp: e.occurred_at ?? null,
      actor: actor.display || actor.id || (actor.type ? String(actor.type) : "system"),
      actor_type: actor.type,
      action: e.action,
      category: e.action_category ?? "",
      outcome: e.outcome,
      reason: e.reason ?? null,
      resource: resource.type ? `${resource.type}${resource.id ? ":" + resource.id : ""}` : null,
      ip: e.source_ip ?? null,
      sequence_no: e.sequence_no,
    };
  });
  return { data, next_cursor: (body as Obj)?.next_cursor ?? null };
}

/**
 * Policy simulation: receiver POST /v1/simulate returns
 * { summary: { scanned, matched, match_rate, elapsed_ms }, matches: [...] }.
 * The policy editor reads evaluated / would_flag, so map them.
 */
export function mapSimulate(body: unknown): unknown {
  const b = (body || {}) as Obj;
  const s = (b.summary || {}) as Obj;
  return {
    evaluated: num(s.scanned),
    would_flag: num(s.matched),
    match_rate: num(s.match_rate),
    elapsed_ms: num(s.elapsed_ms),
    matches: asArray(b.matches),
  };
}

/** Single policy (bare object from GET /v1/policies/{id}) -> { data: {...} }
 *  with the same field names the list uses, so the detail page reads status/cel. */
export function mapPolicyDetail(body: unknown): unknown {
  const p = (body || {}) as Obj;
  const enabled = p.enabled !== false;
  const shadow = p.shadow === true;
  return {
    data: {
      id: p.id,
      name: p.name,
      description: p.description ?? "",
      action: p.action,
      priority: num(p.priority),
      status: !enabled ? "disabled" : shadow ? "shadow" : "enabled",
      cel: p.match_expression,
      action_config: p.action_config ?? {},
      applies_to: p.applies_to ?? [],
      match_count: num(p.match_count),
      last_modified: p.updated_at ?? p.created_at ?? null,
    },
  };
}

/**
 * Trace tree: receiver GET /v1/traces/{id}/tree returns
 * { trace, root, span_count } where root is a SpanNode (or list) with nested
 * children, ISO start/end times, duration_ms, status_code, agent/tool names.
 * The waterfall page wants a FLAT list of
 *   { id, parent, depth, name, service, start(ms rel), dur(ms), status }
 * plus trace meta { id, agent, operation, status, started, spans }.
 * Flatten depth-first, normalize timing relative to the earliest span, and
 * assign a stable per-(agent|tool) "service" index for lane colouring.
 */
export function mapTraceTree(body: unknown): unknown {
  const b = (body || {}) as Obj;
  const trace = (b.trace || {}) as Obj;

  // Flatten the node tree depth-first.
  const flat: Obj[] = [];
  const roots = Array.isArray(b.root) ? (b.root as Obj[]) : b.root ? [b.root as Obj] : [];
  const walk = (node: Obj, parentId: string | null, depth: number) => {
    if (!node) return;
    flat.push({ ...node, _parent: parentId, _depth: depth });
    const kids = Array.isArray(node.children) ? (node.children as Obj[]) : [];
    for (const k of kids) walk(k, (node.span_id as string) ?? null, depth + 1);
  };
  for (const r of roots) walk(r, null, 0);

  const parseMs = (iso: unknown): number => {
    if (!iso) return NaN;
    const t = Date.parse(String(iso));
    return Number.isFinite(t) ? t : NaN;
  };
  const starts = flat.map((n) => parseMs(n.start_time)).filter((n) => Number.isFinite(n));
  const t0 = starts.length ? Math.min(...starts) : 0;

  // Assign a service index per distinct agent/tool/model label.
  const serviceIndex = new Map<string, number>();
  const serviceFor = (label: string): number => {
    if (!serviceIndex.has(label)) serviceIndex.set(label, serviceIndex.size);
    return serviceIndex.get(label)!;
  };

  const statusMap = (code: unknown): "ok" | "blocked" | "error" => {
    const c = String(code || "").toLowerCase();
    if (c.includes("block") || c.includes("denied")) return "blocked";
    if (c.includes("error") || c === "2" || c === "status_code_error") return "error";
    return "ok";
  };


  const spans = flat.map((n) => {
    const startMs = parseMs(n.start_time);
    const dur = typeof n.duration_ms === "number" ? n.duration_ms : (() => {
      const end = parseMs(n.end_time);
      return Number.isFinite(startMs) && Number.isFinite(end) ? Math.max(0, end - startMs) : 0;
    })();
    const label = (n.agent_name as string) || (n.request_model as string) || (n.tool_name as string) || (n.operation_name as string) || "span";
    // intervention_state of "blocked"/"halted"/"denied" means a policy stopped
    // this span. The policy name is carried in attributes.policy_name (the
    // instrumentation tags every decision with it). halt_reason is a free-text
    // explanation. We prefer policy_name for the link/UI label, fall back to
    // halt_reason.
    const interv = String(n.intervention_state || "").toLowerCase();
    const isBlocked = interv.includes("block") || interv.includes("denied") || interv.includes("halt");
    const policyName = (n.attributes && typeof n.attributes === "object"
      ? (n.attributes as Obj)["policy_name"] || (n.attributes as Obj)["strathon.policy.name"]
      : undefined) as string | undefined;
    return {
      id: n.span_id,
      parent: n._parent ?? null,
      depth: num(n._depth),
      name: (n.name as string) || (n.operation_name as string) || label,
      service: serviceFor(String(label)),
      service_name: String(label),
      kind: spanKind(n),
      start: Number.isFinite(startMs) ? startMs - t0 : 0,
      dur: num(dur),
      status: isBlocked ? ("blocked" as const) : statusMap(n.status_code),
      // Pass through the real fields the detail sheet needs. Keeping these on
      // the mapped span avoids a second fetch when the user opens a span.
      tool_name: (n.tool_name as string) || undefined,
      request_model: (n.request_model as string) || undefined,
      provider_name: (n.provider_name as string) || undefined,
      input_tokens: typeof n.input_tokens === "number" ? n.input_tokens : undefined,
      output_tokens: typeof n.output_tokens === "number" ? n.output_tokens : undefined,
      cost_usd: n.cost_usd != null ? String(n.cost_usd) : undefined,
      blockedBy: isBlocked ? (policyName || (n.halt_reason as string) || "policy") : undefined,
      halt_reason: (n.halt_reason as string) || undefined,
      status_message: (n.status_message as string) || undefined,
      attributes: (n.attributes && typeof n.attributes === "object")
        ? (n.attributes as Obj)
        : {},
    };
  });

  const startedNano = num(trace.start_time_unix_nano);
  return {
    data: {
      id: trace.trace_id ?? b.trace_id ?? null,
      agent: trace.agent_name ?? "",
      operation: trace.workflow_name ?? trace.agent_name ?? "",
      status: spans.some((s) => s.status === "blocked") ? "blocked" : spans.some((s) => s.status === "error") ? "error" : "ok",
      started: startedNano ? new Date(startedNano / 1e6).toISOString() : "",
      spans: num(trace.span_count) || spans.length,
      waterfall_spans: spans,
    },
  };
}

/**
 * Spans list: receiver GET /v1/spans returns { data: [SpanRead] } with
 * status_code, start_time/end_time (ISO), agent_name/tool_name, and
 * intervention_state. The spans page reads name, service_name, dur (ms),
 * status (ok|blocked|error), started. Map accordingly.
 */
export function mapSpans(body: unknown): unknown {
  const rows = asArray((body as Obj)?.data ?? (body as Obj)?.spans);


  const data = rows.map((s) => {
    const start = s.start_time ? Date.parse(String(s.start_time)) : NaN;
    const end = s.end_time ? Date.parse(String(s.end_time)) : NaN;
    const dur = Number.isFinite(start) && Number.isFinite(end) ? Math.max(0, end - start) : 0;
    const intervened = String(s.intervention_state || "").toLowerCase();
    const code = String(s.status_code || "").toLowerCase();
    const status = intervened.includes("block") || intervened.includes("halt") || intervened.includes("denied") ? "blocked"
      : code.includes("error") ? "error" : "ok";
    // Service label: prefer agent_name (the app), then model, then tool. This
    // matches mapTraceTree so the same span shows the same Service in both views.
    const label = (s.agent_name as string) || (s.request_model as string) || (s.tool_name as string) || (s.operation_name as string) || "";
    // Tokens + cost: SpanRead has them as nested objects {input, output, total}
    // and a string-typed cost (decimal precision). Surface them flat for the
    // list table to render without a second fetch.
    const tokens = (s.tokens as Obj) || {};
    const cost = (s.cost as Obj) || {};
    return {
      id: s.span_id,
      span_id: s.span_id,
      trace_id: s.trace_id,
      name: s.name || s.operation_name || "span",
      service: label,
      service_name: label,
      kind: spanKind(s),
      dur: Math.round(dur),
      status,
      started: s.start_time ?? null,
      start_time: s.start_time ?? null,
      input_tokens: typeof tokens.input_tokens === "number" ? tokens.input_tokens : undefined,
      output_tokens: typeof tokens.output_tokens === "number" ? tokens.output_tokens : undefined,
      cost_usd: cost.cost_usd != null ? String(cost.cost_usd) : undefined,
    };
  });
  return { data, next_cursor: (body as Obj)?.next_cursor ?? null };
}

/**
 * Trace list: receiver GET /v1/traces returns { data: [TraceResponse] } with
 * trace_id, agent_name, workflow_name, start_time_unix_nano/end_time_unix_nano,
 * span_count, total_cost_usd, intervention_state. The list page reads id,
 * shortId, agent, operation, spans, durationMs, status, started.
 */
export function mapTraces(body: unknown): unknown {
  const rows = asArray((body as Obj)?.data ?? (body as Obj)?.traces);
  const data = rows.map((t) => {
    const startNano = num(t.start_time_unix_nano);
    const endNano = num(t.end_time_unix_nano);
    const durMs = startNano && endNano ? Math.round((endNano - startNano) / 1e5) / 10 : 0;
    const intervened = String(t.intervention_state || "").toLowerCase();
    const status = intervened.includes("block") || intervened.includes("halt") ? "blocked"
      : intervened.includes("error") ? "error" : "ok";
    const tid = String(t.trace_id ?? t.id ?? "");
    return {
      id: tid,
      shortId: tid.slice(0, 16),
      agent: t.agent_name ?? "",
      operation: t.workflow_name ?? t.agent_name ?? "",
      spans: num(t.span_count),
      durationMs: durMs,
      status,
      started: startNano ? new Date(startNano / 1e6).toISOString() : null,
      cost_usd: t.total_cost_usd ?? null,
    };
  });
  return { data, next_cursor: (body as Obj)?.next_cursor ?? null };
}

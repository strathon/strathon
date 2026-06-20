"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { Icons } from "@/components/icons";
import { Badge, Empty, SkeletonTable, Time } from "@/components/ui";
import { useApi } from "@/lib/api-client";
import { KIND_COLOR } from "@/lib/span-colors";

// Kind palette shared with the trace detail waterfall so a kind dot here
// matches the bar colour over there. Blocked is handled by the status badge,
// not the kind dot, since "blocked" overrides the kind for visual emphasis.

interface SpanRow {
  id: string;
  span_id: string;
  trace_id?: string;
  name: string;
  service_name?: string;
  service?: string;
  kind?: keyof typeof KIND_COLOR;
  dur?: number;
  status: "ok" | "blocked" | "error";
  started?: string | null;
  start_time?: string | null;
  input_tokens?: number;
  output_tokens?: number;
  cost_usd?: string;
}

export default function SpansPage() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const params: Record<string, string> = { limit: "50" };
  if (q) params.search = q;
  const { data, loading, error, refetch } = useApi<{ data: SpanRow[] }>("/api/spans", params, [q]);
  const spans = data?.data || [];

  // A few of the rows have token + cost data, so we show those columns when at
  // least one row carries them. Avoids a noisy column of em-dashes when the
  // current page is all tool/agent spans (no tokens, no cost).
  const hasTokens = spans.some((s) => s.input_tokens != null || s.output_tokens != null);
  const hasCost = spans.some((s) => s.cost_usd);

  if (error) return (
    <div className="page">
      <div className="card" style={{ padding: 24, textAlign: "center" }}>
        <div style={{ color: "var(--danger)", marginBottom: 8 }}>{error}</div>
        <button className="btn" onClick={refetch}>Retry</button>
      </div>
    </div>
  );

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1 className="t-h1 page-title">Spans</h1>
          <div className="page-subtitle">All recorded spans across traces.</div>
        </div>
      </div>
      <div className="table-toolbar">
        <div className="input-wrap" style={{ width: 360 }}>
          <Icons.Search size={14} />
          <input className="input search" placeholder="Search operation, service, trace ID…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <div className="grow" />
        <span className="text-muted t-sm">{loading ? "…" : `${spans.length} results`}</span>
      </div>
      <div className="table-wrap">
        {loading ? (
          <SkeletonTable rows={8} columns={[2, 2, 1, 1, 1]} />
        ) : spans.length === 0 ? (
          <Empty
            icon={<Icons.Search size={24} />}
            title={q ? "No matching spans" : "No spans yet"}
            subtitle={q ? "Try a shorter or different query." : "Spans appear when agents are instrumented with the Strathon SDK."}
          />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Span ID</th>
                <th>Operation</th>
                <th>Service</th>
                {hasTokens && <th style={{ textAlign: "right" }}>Tokens</th>}
                {hasCost && <th style={{ textAlign: "right" }}>Cost</th>}
                <th style={{ textAlign: "right" }}>Duration</th>
                <th>Status</th>
                <th>Started</th>
              </tr>
            </thead>
            <tbody>
              {spans.map((s) => {
                const kindColor = KIND_COLOR[s.kind ?? "other"];
                // Row click goes to the parent trace; the spans page is mostly a
                // way to find a span and pivot into its trace. No trace_id means
                // we leave the row passive (rare but possible for orphan spans).
                const onRowClick = s.trace_id ? () => router.push(`/traces/${s.trace_id}`) : undefined;
                return (
                  <tr key={s.id || s.span_id} className={onRowClick ? "clickable" : ""} onClick={onRowClick}>
                    <td className="mono text-secondary" style={{ fontSize: 12 }}>{(s.span_id || s.id).slice(0, 16)}</td>
                    <td>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        {/* Kind dot — matches the trace waterfall colour so a user
                            can scan the list and pick out LLM vs tool vs agent at a glance. */}
                        <span style={{ width: 8, height: 8, borderRadius: 2, background: kindColor, flexShrink: 0 }} title={s.kind ?? "other"} />
                        <span className="mono" style={{ fontSize: 12.5 }}>{s.name}</span>
                      </div>
                    </td>
                    <td className="text-secondary">
                      {s.service_name || s.service || "—"}
                    </td>
                    {hasTokens && (
                      <td className="mono" style={{ textAlign: "right", fontVariantNumeric: "tabular-nums", fontSize: 12, color: "var(--text-muted)" }}>
                        {s.input_tokens != null || s.output_tokens != null
                          ? `${(s.input_tokens ?? 0).toLocaleString()}↓ ${(s.output_tokens ?? 0).toLocaleString()}↑`
                          : "—"}
                      </td>
                    )}
                    {hasCost && (
                      <td className="mono" style={{ textAlign: "right", fontVariantNumeric: "tabular-nums", fontSize: 12 }}>
                        {s.cost_usd ? `$${Number(s.cost_usd).toFixed(4)}` : <span style={{ color: "var(--text-muted)" }}>—</span>}
                      </td>
                    )}
                    <td style={{ fontVariantNumeric: "tabular-nums", textAlign: "right" }}>{s.dur ?? 0}ms</td>
                    <td>
                      <Badge kind={s.status === "ok" ? "success" : s.status === "blocked" ? "danger" : "warning"} dot>
                        {s.status}
                      </Badge>
                    </td>
                    <td className="text-secondary t-sm"><Time ago={s.started || s.start_time || ""} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

"use client";

import { useState, useMemo } from "react";
import { useRouter } from "next/navigation";
import { Icons } from "@/components/icons";
import { StatusBadge, Segmented, Checkbox, Empty, MobileSheet, Time, SkeletonTable } from "@/components/ui";
import { useApi } from "@/lib/api-client";

export default function TracesPage() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [timeRange, setTimeRange] = useState("1h");

  const params: Record<string, string> = { limit: "50" };
  if (q) params.search = q;
  if (statusFilter !== "all") params.status = statusFilter;
  if (timeRange) params.range = timeRange;

  const { data, loading, error, refetch } = useApi<{ data: any[] }>("/api/traces", params, [q, statusFilter, timeRange]);
  const traces = data?.data || [];

  if (error) return <div className="page"><div className="card" style={{ padding: 24, textAlign: "center" }}><div style={{ color: "var(--danger)", marginBottom: 8 }}>{error}</div><button className="btn" onClick={refetch}>Retry</button></div></div>;

  return (
    <div className="page">
      <div className="page-header">
        <div><h1 className="t-h1 page-title">Traces</h1><div className="page-subtitle">{traces.length} traces</div></div>
        <Segmented value={timeRange} onChange={setTimeRange} options={[{ label: "5m", value: "5m" }, { label: "15m", value: "15m" }, { label: "1h", value: "1h" }, { label: "6h", value: "6h" }, { label: "24h", value: "24h" }]} />
      </div>
      <div className="table-toolbar">
        <div className="input-wrap" style={{ width: 360 }}><Icons.Search size={14} /><input className="input search" placeholder="Search agent, operation, trace ID\u2026" value={q} onChange={(e) => setQ(e.target.value)} /></div>
        <div className="grow" />
        <span className="text-muted t-sm">{traces.length} results</span>
      </div>
      <div className="table-wrap">
        {loading ? <SkeletonTable rows={8} columns={[2, 1, 2, 1, 1, 1]} /> : traces.length === 0 ? (
          <Empty icon={<Icons.GitBranch size={24} />} title="No traces yet" subtitle="Connect an agent with the Strathon SDK to see traces here." />
        ) : (
          <table className="table">
            <thead><tr><th>Trace ID</th><th>Agent</th><th>Operation</th><th>Spans</th><th>Duration</th><th>Status</th><th>Started</th></tr></thead>
            <tbody>
              {traces.map((t: any) => (
                <tr key={t.id} className="clickable" onClick={() => router.push(`/traces/${t.id}`)}>
                  <td className="mono text-secondary" style={{ fontSize: 12 }}>{(t.shortId || t.id).slice(0, 16)}</td>
                  <td>{t.agent}</td><td className="mono text-secondary">{t.operation}</td>
                  <td style={{ fontVariantNumeric: "tabular-nums" }}>{t.spans || t.span_count}</td>
                  <td style={{ fontVariantNumeric: "tabular-nums" }}>{t.durationMs || t.duration_ms}ms</td>
                  <td>{StatusBadge[t.status as keyof typeof StatusBadge]?.() || t.status}</td>
                  <td className="text-secondary t-sm"><Time ago={t.started || t.start_time} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

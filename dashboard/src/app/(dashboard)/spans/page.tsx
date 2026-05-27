"use client";
import { useState } from "react";
import { Icons } from "@/components/icons";
import { Badge, Segmented, Empty, SkeletonTable, Time } from "@/components/ui";
import { useApi } from "@/lib/api-client";

export default function SpansPage() {
  const [q, setQ] = useState("");
  const [tab, setTab] = useState("list");
  const params: Record<string, string> = { limit: "50" };
  if (q) params.search = q;
  const { data, loading, error, refetch } = useApi<{ data: any[] }>("/api/spans", params, [q]);
  const spans = data?.data || [];

  if (error) return <div className="page"><div className="card" style={{ padding: 24, textAlign: "center" }}><div style={{ color: "var(--danger)", marginBottom: 8 }}>{error}</div><button className="btn" onClick={refetch}>Retry</button></div></div>;

  return (
    <div className="page">
      <div className="page-header"><div><h1 className="t-h1 page-title">Spans</h1><div className="page-subtitle">{spans.length} spans</div></div>
        <Segmented value={tab} onChange={setTab} options={[{ label: "List", value: "list" }, { label: "Timeseries", value: "timeseries" }, { label: "Top list", value: "toplist" }]} />
      </div>
      <div className="table-toolbar"><div className="input-wrap" style={{ width: 360 }}><Icons.Search size={14} /><input className="input search" placeholder="Search operation, service, trace ID\u2026" value={q} onChange={(e) => setQ(e.target.value)} /></div><div className="grow" /><span className="text-muted t-sm">{spans.length} results</span></div>
      <div className="table-wrap">
        {loading ? <SkeletonTable rows={8} columns={[2, 2, 1, 1, 1]} /> : spans.length === 0 ? (
          <Empty icon={<Icons.Search size={24} />} title="No spans yet" subtitle="Spans appear when agents are instrumented with the Strathon SDK." />
        ) : (
          <table className="table">
            <thead><tr><th>Span ID</th><th>Operation</th><th>Service</th><th>Duration</th><th>Status</th><th>Started</th></tr></thead>
            <tbody>{spans.map((s: any) => (
              <tr key={s.id || s.span_id}><td className="mono text-secondary" style={{ fontSize: 12 }}>{(s.span_id || s.id).slice(0, 16)}</td><td>{s.name || s.operation}</td><td className="text-secondary">{s.service_name || s.service}</td>
                <td style={{ fontVariantNumeric: "tabular-nums" }}>{s.dur || s.duration_ms}ms</td><td><Badge kind={s.status === "ok" ? "success" : s.status === "blocked" ? "danger" : "warning"} dot>{s.status}</Badge></td>
                <td className="text-secondary t-sm"><Time ago={s.started || s.start_time} /></td></tr>
            ))}</tbody>
          </table>
        )}
      </div>
    </div>
  );
}

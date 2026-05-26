"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { Icons } from "@/components/icons";
import { Badge, Ring, Sparkline, Segmented, Empty, SkeletonCards } from "@/components/ui";
import { useApi } from "@/lib/api-client";

export default function AgentsPage() {
  const router = useRouter();
  const [view, setView] = useState("cards");
  const { data, loading, error, refetch } = useApi<{ data: any[] }>("/api/agents");
  const agents = data?.data || [];

  if (error) return <div className="page"><div className="card" style={{ padding: 24, textAlign: "center" }}><div style={{ color: "var(--danger)", marginBottom: 8 }}>{error}</div><button className="btn" onClick={refetch}>Retry</button></div></div>;

  return (
    <div className="page">
      <div className="page-header"><div><h1 className="t-h1 page-title">Agents</h1><div className="page-subtitle">{agents.length} registered</div></div>
        <Segmented value={view} onChange={setView} options={[{ label: "Cards", value: "cards" }, { label: "Table", value: "table" }]} />
      </div>
      {loading ? <SkeletonCards count={6} /> : agents.length === 0 ? (
        <Empty icon={<Icons.Bot size={24} />} title="No agents registered" subtitle="Agents appear automatically when they connect via the SDK." />
      ) : view === "cards" ? (
        <div className="agents-grid">
          {agents.map((a: any) => (
            <div key={a.id} className="card" style={{ cursor: "pointer" }} onClick={() => router.push(`/traces?agent=${a.name}`)}>
              <div style={{ display: "flex", gap: 14, alignItems: "center", marginBottom: 12 }}>
                <Ring value={100 - (a.risk || 0)} size={56} stroke={5} color={a.risk > 70 ? "var(--danger)" : a.risk > 40 ? "var(--warning)" : "var(--success)"} label={a.risk} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontWeight: 600, fontSize: 15 }}>{a.name}</div>
                  <div className="t-sm text-secondary">{a.description || a.owner}</div>
                </div>
                {a.live && <span className="dot-status live" />}
              </div>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap", fontSize: 12.5, color: "var(--text-secondary)" }}>
                <span><strong style={{ color: "var(--text)" }}>{a.calls?.toLocaleString()}</strong> calls</span>
                <span><strong style={{ color: "var(--text)" }}>{a.models}</strong> models</span>
                <span><strong style={{ color: "var(--text)" }}>${a.spend?.toFixed(2)}</strong> spend</span>
                <span><strong style={{ color: "var(--text)" }}>{a.policies}</strong> policies</span>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Name</th><th>Risk</th><th>Calls</th><th>Models</th><th>Spend</th><th>Policies</th><th>Last active</th></tr></thead>
            <tbody>{agents.map((a: any) => (
              <tr key={a.id} className="clickable" onClick={() => router.push(`/traces?agent=${a.name}`)}>
                <td style={{ fontWeight: 500 }}>{a.name}{a.live && <span className="dot-status live" style={{ marginLeft: 8 }} />}</td>
                <td><Badge kind={a.risk > 70 ? "danger" : a.risk > 40 ? "warning" : "success"}>{a.risk}</Badge></td>
                <td style={{ fontVariantNumeric: "tabular-nums" }}>{a.calls?.toLocaleString()}</td><td>{a.models}</td>
                <td style={{ fontVariantNumeric: "tabular-nums" }}>${a.spend?.toFixed(2)}</td><td>{a.policies}</td>
                <td className="text-secondary t-sm">{a.lastActive || a.last_active}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </div>
  );
}

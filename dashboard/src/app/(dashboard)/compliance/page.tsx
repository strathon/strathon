"use client";
import { useRouter } from "next/navigation";
import { Icons } from "@/components/icons";
import { Badge, Ring, Sparkline, Skeleton, Empty, useToast } from "@/components/ui";
import { useApi, api } from "@/lib/api-client";

export default function CompliancePage() {
  const router = useRouter();
  const toast = useToast();
  const { data, loading, error, refetch } = useApi<{ data: { frameworks?: any[]; recommendations?: any[] } }>("/api/compliance");
  const frameworks = data?.data?.frameworks || data?.data || [];
  const recommendations = data?.data?.recommendations || [];

  if (error) return <div className="page"><div className="card" style={{ padding: 24, textAlign: "center" }}><div style={{ color: "var(--danger)", marginBottom: 8 }}>{error}</div><button className="btn" onClick={refetch}>Retry</button></div></div>;

  async function handleExport(format: string) {
    try {
      const res = await fetch("/api/compliance", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ format }) });
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a"); a.href = url; a.download = `strathon-compliance.${format}`; a.click();
      URL.revokeObjectURL(url);
    } catch { toast.push({ tone: "danger", title: "Export failed" }); }
  }

  return (
    <div className="page">
      <div className="page-header"><div><h1 className="t-h1 page-title">Compliance</h1><div className="page-subtitle">Coverage against industry frameworks.</div></div>
        <button className="btn ghost" onClick={() => router.push("/settings?section=export")}><Icons.Settings size={13} /> Manage exports</button>
      </div>
      {loading ? <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))", gap: 16 }}>{[1,2,3,4].map(i => <Skeleton key={i} width="100%" height={200} style={{ borderRadius: 12 }} />)}</div> : (Array.isArray(frameworks) && frameworks.length > 0) ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))", gap: 16, marginBottom: 28 }}>
          {frameworks.map((fw: any) => {
            const color = fw.coverage >= 80 ? "var(--success)" : fw.coverage >= 60 ? "var(--warning)" : "var(--danger)";
            return (
              <div key={fw.id} className="card">
                <div style={{ display: "flex", alignItems: "flex-start", gap: 16 }}>
                  <Ring value={fw.coverage} size={80} stroke={7} color={color} label={`${fw.coverage}%`} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}><h3 className="t-h3">{fw.name}</h3>{fw.coverage >= 80 ? <Badge kind="success" dot>on track</Badge> : <Badge kind="warning" dot>review needed</Badge>}</div>
                    <div className="t-sm text-secondary" style={{ marginBottom: 12 }}>{fw.description}</div>
                    <div style={{ display: "flex", gap: 14, fontSize: 12.5, color: "var(--text-secondary)" }}>
                      <span><b style={{ color: "var(--text)" }}>{fw.controls}</b> controls</span>
                      <span><b style={{ color: "var(--warning)" }}>{fw.recs}</b> recommendation{fw.recs !== 1 && "s"}</span>
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      ) : <Empty icon={<Icons.FileCheck size={24} />} title="No frameworks configured" subtitle="Configure compliance frameworks in settings." />}
      {recommendations.length > 0 && (
        <>
          <h2 className="t-h2" style={{ marginBottom: 14 }}>Recommendations</h2>
          <div className="col" style={{ gap: 10 }}>
            {recommendations.map((r: any, i: number) => (
              <div key={i} className="card dense" style={{ borderLeftWidth: 3, borderLeftStyle: "solid", borderLeftColor: "var(--warning)", paddingLeft: 18, display: "flex", alignItems: "center", gap: 16 }}>
                <Icons.AlertTriangle size={18} stroke="var(--warning)" />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}><Badge kind="warning">{r.framework}</Badge><span style={{ fontWeight: 600 }}>{r.title}</span></div>
                  <div className="t-sm text-secondary" style={{ marginTop: 4 }}>{r.detail}</div>
                </div>
                <button className="btn primary sm" onClick={() => toast.push({ tone: "success", title: "Recommendation actioned" })}><Icons.Zap size={12} /> {r.cta}</button>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

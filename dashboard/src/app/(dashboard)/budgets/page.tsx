"use client";
import { Icons } from "@/components/icons";
import { Badge, StatusBadge, Segmented, AreaChart, CountUp, Skeleton, Empty } from "@/components/ui";
import { useApi } from "@/lib/api-client";

export default function BudgetsPage() {
  const { data, loading, error, refetch } = useApi<{ data: any }>("/api/budgets");
  const budgets = data?.data || data;

  if (error) return <div className="page"><div className="card" style={{ padding: 24, textAlign: "center" }}><div style={{ color: "var(--danger)", marginBottom: 8 }}>{error}</div><button className="btn" onClick={refetch}>Retry</button></div></div>;

  const series = budgets?.series || [];
  const rules = budgets?.rules || [];
  const agents = budgets?.agents || ["atlas", "orca", "nova", "kepler", "helix"];
  const colors = ["var(--svc-2)", "var(--svc-1)", "var(--svc-3)", "var(--svc-5)", "var(--svc-4)", "var(--svc-6)"];
  const spendMtd = budgets?.spend_mtd || 0;
  const forecast = budgets?.forecast || 0;
  const headroom = budgets?.headroom || 0;
  const activeRules = budgets?.active_rules || rules.length;
  const daily = budgets?.daily || [];
  const stackedSeries = agents.map((_: string, ai: number) => series.map((d: any) => d?.[agents[ai]] || 0));

  return (
    <div className="page">
      <div className="page-header"><div><h1 className="t-h1 page-title">Budgets</h1><div className="page-subtitle">Track model spend with forecasted EOM and alerts.</div></div><button className="btn primary"><Icons.Plus size={13} /> New budget rule</button></div>
      <div className="kpi-grid">
        <div className="kpi"><span className="kpi-label">Spend &middot; month to date</span><span className="kpi-value">{loading ? <Skeleton width={60} height={28} /> : <>$<CountUp to={spendMtd} format={(n) => n.toFixed(2)} /></>}</span></div>
        <div className="kpi"><span className="kpi-label">Forecast &middot; end of month</span><span className="kpi-value">{loading ? <Skeleton width={50} height={28} /> : <>$<CountUp to={forecast} /></>}</span></div>
        <div className="kpi"><span className="kpi-label">Headroom</span><span className="kpi-value" style={{ color: "var(--warning)" }}>{loading ? <Skeleton width={40} height={28} /> : <CountUp to={headroom} format={(n) => Math.round(n) + "%"} />}</span></div>
        <div className="kpi"><span className="kpi-label">Active rules</span><span className="kpi-value">{loading ? <Skeleton width={24} height={28} /> : <CountUp to={activeRules} />}</span></div>
      </div>
      {stackedSeries[0]?.length > 0 && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-header"><span className="card-title">Spend by agent &middot; last 30 days</span></div>
          <AreaChart series={stackedSeries} height={220} colors={colors} />
        </div>
      )}
      {rules.length > 0 && (
        <div className="card">
          <div className="card-header"><span className="card-title">Budget rules</span></div>
          <div className="table-wrap" style={{ border: "none" }}>
            <table className="table" style={{ background: "transparent" }}>
              <thead><tr><th>Name</th><th>Scope</th><th>Threshold</th><th>Period</th><th>Action</th><th>Status</th></tr></thead>
              <tbody>{rules.map((r: any, i: number) => (
                <tr key={r.id || i}><td style={{ fontWeight: 500 }}>{r.name}</td><td>{r.scope}</td><td className="mono">{r.threshold}</td><td>{r.period}</td>
                  <td><Badge kind={r.action === "block" ? "danger" : r.action === "throttle" ? "warning" : "info"} mono>{r.action}</Badge></td>
                  <td>{r.status === "enabled" ? StatusBadge.enabled() : StatusBadge.shadow()}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

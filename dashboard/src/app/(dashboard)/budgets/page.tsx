"use client";
import { useState } from "react";
import { Icons } from "@/components/icons";
import { Badge, StatusBadge, Segmented, AreaChart, CountUp, Skeleton, Empty, Modal, Dropdown, useToast } from "@/components/ui";
import { useApi, api } from "@/lib/api-client";

export default function BudgetsPage() {
  const { data, loading, error, refetch } = useApi<{ data: any }>("/api/budgets");
  const { data: forecastData } = useApi<{ data: { forecast?: number; headroom?: number | null; burn_rate_usd_per_hour?: number } }>("/api/budgets/forecast");
  const { data: spendSeriesData } = useApi<{ data: { agents?: string[]; series?: Array<Record<string, number>> } }>("/api/budgets/spend-series");
  const budgets = data?.data || data;
  const toast = useToast();

  const [showCreate, setShowCreate] = useState(false);
  const [bName, setBName] = useState("");
  const [bScope, setBScope] = useState("project");
  const [bScopeValue, setBScopeValue] = useState("");
  const [bAmount, setBAmount] = useState("");
  const [bDuration, setBDuration] = useState("30d");
  const [creating, setCreating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [editTarget, setEditTarget] = useState<{ id: string; name: string; threshold: string; kind: string } | null>(null);
  const [editName, setEditName] = useState("");
  const [editAmount, setEditAmount] = useState("");
  const [savingEdit, setSavingEdit] = useState(false);

  function openEdit(r: any) {
    setEditTarget({ id: r.id, name: r.name, threshold: String(r.threshold), kind: r.kind });
    setEditName(r.name);
    setEditAmount(String(r.threshold));
  }

  async function saveEdit() {
    if (!editTarget || savingEdit) return;
    if (!editName.trim()) { toast.push({ tone: "danger", title: "Name is required" }); return; }
    setSavingEdit(true);
    try {
      const body: Record<string, unknown> = { name: editName.trim() };
      // Only cost budgets have a USD spend limit; iteration budgets cap calls.
      if (editTarget.kind !== "iteration") body.max_spend_usd = editAmount;
      await api.patch(`/api/budgets/${encodeURIComponent(editTarget.id)}`, body);
      toast.push({ tone: "success", title: "Budget updated", body: editName.trim() });
      setEditTarget(null);
      refetch();
    } catch (e) {
      toast.push({ tone: "danger", title: "Failed to update budget", body: e instanceof Error ? e.message : "Unknown error" });
    } finally {
      setSavingEdit(false);
    }
  }

  async function deleteBudget() {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    try {
      await api.del(`/api/budgets/${encodeURIComponent(deleteTarget.id)}`);
      toast.push({ tone: "success", title: "Budget deleted", body: deleteTarget.name });
      setDeleteTarget(null);
      refetch();
    } catch (e) {
      toast.push({ tone: "danger", title: "Failed to delete budget", body: e instanceof Error ? e.message : "Unknown error" });
    } finally {
      setDeleting(false);
    }
  }

  async function createBudget() {
    if (!bName.trim()) { toast.push({ tone: "danger", title: "Name is required" }); return; }
    if (!bAmount || Number(bAmount) <= 0) { toast.push({ tone: "danger", title: "Enter a spend limit greater than 0" }); return; }
    setCreating(true);
    try {
      await api.post("/api/budgets", {
        name: bName.trim(),
        scope: bScope,
        scope_value: bScope === "project" ? null : (bScopeValue.trim() || null),
        kind: "cost",
        max_spend_usd: bAmount,
        budget_duration: bDuration,
      });
      toast.push({ tone: "success", title: "Budget created", body: bName });
      setShowCreate(false);
      setBName(""); setBScopeValue(""); setBAmount("");
      refetch();
    } catch (e) {
      toast.push({ tone: "danger", title: "Failed to create budget", body: e instanceof Error ? e.message : "Unknown error" });
    } finally {
      setCreating(false);
    }
  }

  if (error) return <div className="page"><div className="card" style={{ padding: 24, textAlign: "center" }}><div style={{ color: "var(--danger)", marginBottom: 8 }}>{error}</div><button className="btn" onClick={refetch}>Retry</button></div></div>;

  const series = spendSeriesData?.data?.series || [];
  const rules = budgets?.rules || [];
  const agents = spendSeriesData?.data?.agents || [];
  const colors = ["var(--svc-2)", "var(--svc-1)", "var(--svc-3)", "var(--svc-5)", "var(--svc-4)", "var(--svc-6)"];
  const spendMtd = budgets?.spend_mtd || 0;
  const forecast = forecastData?.data?.forecast ?? 0;
  // Headroom is null when there are no budgets to compare against; keep it null
  // (not 0) so the KPI shows a muted dash rather than an alarming red 0%.
  const headroom = forecastData?.data?.headroom ?? null;
  const activeRules = budgets?.active_rules || rules.length;
  const stackedSeries = agents.map((_: string, ai: number) => series.map((d: any) => d?.[agents[ai]] || 0));

  return (
    <div className="page">
      <div className="page-header"><div><h1 className="t-h1 page-title">Budgets</h1><div className="page-subtitle">Track model spend with forecasted EOM and alerts.</div></div><button className="btn primary" onClick={() => setShowCreate(true)}><Icons.Plus size={13} /> New budget rule</button></div>
      <div className="kpi-grid">
        <div className="kpi"><span className="kpi-label">Spend &middot; month to date</span><span className="kpi-value">{loading ? <Skeleton width={60} height={28} /> : <>$<CountUp to={spendMtd} format={(n) => n.toFixed(2)} /></>}</span></div>
        <div className="kpi"><span className="kpi-label">Forecast &middot; end of month</span><span className="kpi-value">{loading ? <Skeleton width={50} height={28} /> : <>$<CountUp to={forecast} /></>}</span></div>
        <div className="kpi">
          <span className="kpi-label">Headroom</span>
          <span className="kpi-value" style={{
            // Headroom is the share of budget not yet spent. It's null when no
            // active rules exist (nothing to compare against) — show a muted
            // dash, not an alarming 0%. When rules exist, threshold-colour it.
            color: headroom === null
              ? "var(--text-muted)"
              : headroom >= 50 ? "var(--success)"
              : headroom >= 25 ? "var(--warning)"
              : "var(--danger)"
          }}>
            {loading ? <Skeleton width={40} height={28} /> : headroom === null ? "—" : <CountUp to={headroom} format={(n) => Math.round(n) + "%"} />}
          </span>
        </div>
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
              <thead><tr><th>Name</th><th>Scope</th><th>Threshold</th><th>Period</th><th>Type</th><th>Status</th><th style={{ width: 40 }}></th></tr></thead>
              <tbody>{rules.map((r: any, i: number) => (
                <tr key={r.id || i}><td style={{ fontWeight: 500 }}>{r.name}</td><td>{r.scope}</td><td className="mono">{r.threshold}</td><td>{r.period}</td>
                  <td><Badge kind={r.kind === "iteration" ? "info" : "accent"} mono>{r.kind === "iteration" ? "iteration" : "cost"}</Badge></td>
                  <td>{r.status === "enabled" ? StatusBadge.enabled() : StatusBadge.shadow()}</td>
                  <td style={{ textAlign: "right" }}>
                    {r.id && (
                      <Dropdown align="right" width={150}
                        trigger={({ toggle }) => <button className="btn icon ghost sm" onClick={toggle} aria-label="Budget actions"><Icons.MoreHorizontal size={14} /></button>}
                        items={[
                          { icon: <Icons.Edit size={13} />, label: "Edit", onClick: () => openEdit(r) },
                          { icon: <Icons.Trash size={13} />, label: "Delete", danger: true, onClick: () => setDeleteTarget({ id: r.id, name: r.name }) },
                        ]} />
                    )}
                  </td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </div>
      )}
      <Modal open={showCreate} onClose={() => setShowCreate(false)} title="New budget rule"
        confirmLabel={creating ? "Creating…" : "Create budget"} onConfirm={createBudget}
        body={
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <span className="t-sm text-muted">Name</span>
              <input className="input" value={bName} onChange={(e) => setBName(e.target.value)} placeholder="Monthly model spend" />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <span className="t-sm text-muted">Scope</span>
              <select className="input" value={bScope} onChange={(e) => setBScope(e.target.value)}>
                <option value="project">Whole project</option>
                <option value="agent">Specific agent</option>
                <option value="model">Specific model</option>
              </select>
            </label>
            {bScope !== "project" && (
              <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <span className="t-sm text-muted">{bScope === "agent" ? "Agent name" : "Model name"}</span>
                <input className="input" value={bScopeValue} onChange={(e) => setBScopeValue(e.target.value)} placeholder={bScope === "agent" ? "atlas" : "gpt-4o"} />
              </label>
            )}
            <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <span className="t-sm text-muted">Spend limit (USD)</span>
              <input className="input" type="number" min="0" step="0.01" value={bAmount} onChange={(e) => setBAmount(e.target.value)} placeholder="100.00" />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <span className="t-sm text-muted">Period</span>
              <select className="input" value={bDuration} onChange={(e) => setBDuration(e.target.value)}>
                <option value="1d">Daily</option>
                <option value="7d">Weekly</option>
                <option value="30d">Monthly</option>
              </select>
            </label>
          </div>
        } />
      <Modal open={!!deleteTarget} onClose={() => setDeleteTarget(null)} danger
        title="Delete budget rule?" confirmLabel={deleting ? "Deleting…" : "Delete"}
        onConfirm={deleteBudget}
        body={<>Delete <strong>{deleteTarget?.name}</strong>? This stops tracking spend against this budget. This cannot be undone.</>} />
      <Modal open={!!editTarget} onClose={() => setEditTarget(null)} title="Edit budget rule"
        confirmLabel={savingEdit ? "Saving…" : "Save changes"} onConfirm={saveEdit}
        body={
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <span className="t-sm text-muted">Name</span>
              <input className="input" value={editName} onChange={(e) => setEditName(e.target.value)} autoFocus />
            </label>
            {editTarget?.kind !== "iteration" && (
              <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <span className="t-sm text-muted">Spend limit (USD)</span>
                <input className="input" type="number" min="0" step="0.01" value={editAmount} onChange={(e) => setEditAmount(e.target.value)} />
              </label>
            )}
            <span className="t-sm text-muted">Scope and period can&apos;t be changed — they&apos;d invalidate tracked spend. To change those, delete this rule and create a new one.</span>
          </div>
        } />
    </div>
  );
}

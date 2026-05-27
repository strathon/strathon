"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { Icons } from "@/components/icons";
import { Badge, StatusBadge, Sparkline, Ring, CountUp, SkeletonTable, Skeleton, useToast } from "@/components/ui";
import { useApi, api } from "@/lib/api-client";

export default function OverviewPage() {
  const router = useRouter();
  const toast = useToast();

  const { data: policiesData, loading: pLoading, error: pError } = useApi<{ data: Array<{ id: string; name: string; status: string; action: string; hits7d: number[]; priority: number }> }>("/api/policies");
  const { data: tracesData, loading: tLoading, error: tError } = useApi<{ data: Array<{ id: string; shortId?: string; agent: string; operation: string; status: string; started: string }> }>("/api/traces", { limit: "6" });
  const { data: approvalsData, loading: aLoading, error: aError } = useApi<{ data: Array<{ id: string; agent: string; tool: string; expiresIn: number }> }>("/api/approvals");
  const { data: budgetsData, loading: bLoading, error: bError } = useApi<{ data: { spend_mtd?: number; daily?: number[] } }>("/api/budgets");
  const { data: complianceData } = useApi<{ data: Array<{ id: string; name: string; coverage: number }> }>("/api/compliance");

  const policies = policiesData?.data || [];
  const traces = tracesData?.data || [];
  const approvals = approvalsData?.data || [];
  const budgets = budgetsData?.data;
  const frameworks = complianceData?.data || [];

  const enabled = policies.filter((p) => p.status === "enabled").length;
  const shadow = policies.filter((p) => p.status === "shadow").length;
  const disabled = policies.filter((p) => p.status === "disabled").length;
  const blockedToday = policies.filter((p) => p.action === "block").reduce((a, p) => a + (p.hits7d?.[p.hits7d.length - 1] || 0), 0);
  const spendMtd = budgets?.spend_mtd || 0;
  const spendDaily = budgets?.daily || [];

  // Derive throughput from real trace data
  const recentSpanCount = traces.reduce((a: number, t: any) => a + (t.spans || t.span_count || 0), 0);
  const topPolicies = [...policies].sort((a, b) => (b.hits7d?.reduce((x, y) => x + y, 0) || 0) - (a.hits7d?.reduce((x, y) => x + y, 0) || 0)).slice(0, 4);

  const allLoading = pLoading || tLoading || aLoading || bLoading;
  const allError = !allLoading && pError && tError && aError && bError;

  if (allError) return (
    <div className="page">
      <div className="page-header"><div><h1 className="t-h1 page-title">Overview</h1></div></div>
      <div className="card" style={{ padding: 32, textAlign: "center" }}>
        <Icons.AlertTriangle size={32} style={{ color: "var(--danger)", marginBottom: 12 }} />
        <div style={{ fontWeight: 600, fontSize: 15, marginBottom: 6 }}>Cannot reach Strathon receiver</div>
        <div className="t-sm text-secondary" style={{ marginBottom: 16 }}>Is the receiver running? Check that your server is started.</div>
        <button className="btn" onClick={() => window.location.reload()}>Retry</button>
      </div>
    </div>
  );

  // Welcome banner (show when no policies and no api keys, dismissible)
  const [bannerDismissed, setBannerDismissed] = useState(false);
  useEffect(() => { try { if (localStorage.getItem("strathon-welcome-dismissed") === "1") setBannerDismissed(true); } catch {} }, []);
  const showWelcome = !bannerDismissed && !pLoading && policies.length === 0;

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1 className="t-h1 page-title">Overview</h1>
          <div className="page-subtitle">Live posture across your AI agent firewall.</div>
        </div>
      </div>

      {showWelcome && (
        <div className="card" style={{ marginBottom: 16, background: "var(--accent-bg)", border: "1px solid var(--accent-border)", position: "relative" }}>
          <button className="btn icon ghost sm" style={{ position: "absolute", top: 8, right: 8 }} onClick={() => { setBannerDismissed(true); try { localStorage.setItem("strathon-welcome-dismissed", "1"); } catch {} }}><Icons.X size={14} /></button>
          <div style={{ fontWeight: 600, fontSize: 15, marginBottom: 8 }}>Welcome to Strathon!</div>
          <div className="t-sm text-secondary" style={{ marginBottom: 14 }}>Get started in three steps:</div>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center" }}>
            <span className="t-sm"><strong>1.</strong></span>
            <button className="btn sm" onClick={() => router.push("/policies")}><Icons.Shield size={12} /> Create your first policy</button>
            <span className="t-sm"><strong>2.</strong></span>
            <button className="btn sm" onClick={() => router.push("/settings?section=apikeys")}><Icons.Key size={12} /> Get your API key</button>
            <span className="t-sm"><strong>3.</strong></span>
            <span className="mono t-sm" style={{ padding: "4px 8px", background: "var(--bg-input)", borderRadius: 4 }}>pip install strathon</span>
          </div>
        </div>
      )}

      <div className="kpi-grid">
        <div className="kpi">
          <span className="kpi-label">Spans &middot; recent</span>
          <span className="kpi-value" style={{ fontVariantNumeric: "tabular-nums" }}>{allLoading ? <Skeleton width={40} height={28} /> : <CountUp to={recentSpanCount} />}</span>
          <span className="kpi-meta">across {traces.length} traces</span>
        </div>
        <div className="kpi">
          <span className="kpi-label">Blocked &middot; today</span>
          <span className="kpi-value" style={{ color: "var(--danger)" }}>{allLoading ? <Skeleton width={40} height={28} /> : <CountUp to={blockedToday} />}</span>
          <span className="kpi-meta">{enabled} enforcing policies</span>
        </div>
        <div className="kpi">
          <span className="kpi-label">Pending approvals</span>
          <span className="kpi-value" style={{ color: approvals.length > 0 ? "var(--warning)" : "var(--text)" }}>{allLoading ? <Skeleton width={24} height={28} /> : <CountUp to={approvals.length} />}</span>
          <span className="kpi-meta">{approvals.length > 0 ? "needs your decision" : "all clear"}</span>
        </div>
        <div className="kpi">
          <span className="kpi-label">Spend &middot; month to date</span>
          <span className="kpi-value">{allLoading ? <Skeleton width={60} height={28} /> : <>$<CountUp to={spendMtd} format={(n) => n.toFixed(0)} /></>}</span>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))", gap: 16, marginTop: 16 }}>
        <div className="card">
          <div className="card-header">
            <span className="card-title">Needs your attention</span>
            <button className="btn ghost sm" onClick={() => router.push("/approvals")}>View all <Icons.ArrowRight size={12} /></button>
          </div>
          {aLoading ? <Skeleton width="100%" height={80} /> : approvals.length === 0 ? (
            <div className="t-sm text-muted" style={{ padding: "16px 0" }}>Nothing pending. You&apos;re all caught up.</div>
          ) : (
            <div className="col" style={{ gap: 8 }}>
              {approvals.slice(0, 4).map((a) => (
                <div key={a.id} onClick={() => router.push("/approvals")} style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 12px", borderRadius: 8, background: "var(--bg-input)", cursor: "pointer" }}>
                  <div style={{ width: 30, height: 30, borderRadius: 7, background: "var(--warning-bg)", color: "var(--warning)", display: "grid", placeItems: "center", flexShrink: 0 }}><Icons.UserCheck size={14} /></div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 600, fontSize: 13 }}>{a.agent}</div>
                    <div className="t-sm text-muted mono" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{a.tool}</div>
                  </div>
                  {a.expiresIn > 0 && <span className="t-sm text-secondary" style={{ flexShrink: 0 }}>{Math.floor(a.expiresIn / 60)}m left</span>}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-header">
            <span className="card-title">Recent activity</span>
            <button className="btn ghost sm" onClick={() => router.push("/traces")}>All traces <Icons.ArrowRight size={12} /></button>
          </div>
          {tLoading ? <Skeleton width="100%" height={120} /> : traces.length === 0 ? (
            <div className="t-sm text-muted" style={{ padding: "16px 0" }}>No traces yet. Connect an agent to start.</div>
          ) : (
            <div className="col" style={{ gap: 6 }}>
              {traces.map((t) => (
                <div key={t.id} onClick={() => router.push(`/traces/${t.id}`)} style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 10px", borderRadius: 6, cursor: "pointer", fontSize: 12.5 }}>
                  <span className="dot-status" style={{ background: t.status === "blocked" ? "var(--danger)" : t.status === "error" ? "var(--warning)" : "var(--success)", flexShrink: 0 }} />
                  <span className="mono text-secondary" style={{ flexShrink: 0 }}>{(t.shortId || t.id).slice(0, 10)}</span>
                  <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.agent}</span>
                  <span className="text-muted mono" style={{ flexShrink: 0 }}>{t.operation}</span>
                  <span className="text-muted" style={{ flexShrink: 0, marginLeft: 4 }}>{t.started}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 16, marginTop: 16 }}>
        <div className="card">
          <div className="card-header">
            <span className="card-title">Policy health</span>
            <button className="btn ghost sm" onClick={() => router.push("/policies")}>Manage <Icons.ArrowRight size={12} /></button>
          </div>
          <div style={{ display: "flex", gap: 14, marginBottom: 14 }}>
            <span style={{ display: "flex", alignItems: "center", gap: 6 }}><span className="dot-status" style={{ background: "var(--success)" }} /> {enabled} enabled</span>
            <span style={{ display: "flex", alignItems: "center", gap: 6 }}><span className="dot-status" style={{ background: "var(--warning)" }} /> {shadow} shadow</span>
            <span style={{ display: "flex", alignItems: "center", gap: 6 }}><span className="dot-status" style={{ background: "var(--text-muted)" }} /> {disabled} disabled</span>
          </div>
          {topPolicies.length > 0 && (
            <>
              <div className="t-caption text-muted" style={{ marginBottom: 8 }}>Most active &middot; last 7 days</div>
              <div className="col" style={{ gap: 6 }}>
                {topPolicies.map((p) => (
                  <div key={p.id} onClick={() => router.push(`/policies/${p.id}`)} style={{ display: "flex", alignItems: "center", gap: 10, padding: "6px 8px", borderRadius: 6, cursor: "pointer" }}>
                    <Badge kind={p.action === "block" ? "danger" : p.action === "steer" || p.action === "throttle" ? "warning" : p.action === "alert" || p.action === "require_approval" ? "info" : "muted"} mono>{p.action}</Badge>
                    <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 13 }}>{p.name}</span>
                    {p.hits7d && <Sparkline data={p.hits7d} width={56} height={18} color="var(--accent)" />}
                    <span className="t-sm text-muted" style={{ fontVariantNumeric: "tabular-nums", width: 32, textAlign: "right" }}>{p.hits7d?.reduce((a, b) => a + b, 0) || 0}</span>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>

        <div className="card">
          <div className="card-header"><span className="card-title">Spend trend</span></div>
          <div style={{ fontSize: 24, fontWeight: 600, letterSpacing: "-0.02em", marginBottom: 4 }}>${spendMtd.toFixed(0)}</div>
          <div className="t-sm text-muted" style={{ marginBottom: 12 }}>30-day total</div>
          {spendDaily.length > 0 && <Sparkline data={spendDaily} width={240} height={56} color="var(--accent)" valueFormat={(v) => `$${v.toFixed(0)}`} />}
          <button className="btn ghost sm" style={{ marginTop: 12 }} onClick={() => router.push("/budgets")}>Budgets <Icons.ArrowRight size={12} /></button>
        </div>

        <div className="card">
          <div className="card-header"><span className="card-title">Compliance</span></div>
          {frameworks.length === 0 ? (
            <div className="t-sm text-muted" style={{ padding: "12px 0" }}>No frameworks configured.</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {frameworks.slice(0, 4).map((fw) => {
                const color = fw.coverage >= 80 ? "var(--success)" : fw.coverage >= 60 ? "var(--warning)" : "var(--danger)";
                return (
                  <div key={fw.id} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <span style={{ flex: 1, fontSize: 13 }}>{fw.name}</span>
                    <div style={{ width: 80, height: 5, borderRadius: 3, background: "var(--bg-input)", overflow: "hidden" }}>
                      <div style={{ width: `${fw.coverage}%`, height: "100%", background: color }} />
                    </div>
                    <span className="t-sm" style={{ color, fontVariantNumeric: "tabular-nums", width: 34, textAlign: "right" }}>{fw.coverage}%</span>
                  </div>
                );
              })}
            </div>
          )}
          <button className="btn ghost sm" style={{ marginTop: 14 }} onClick={() => router.push("/compliance")}>Details <Icons.ArrowRight size={12} /></button>
        </div>
      </div>
    </div>
  );
}

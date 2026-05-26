"use client";

import { useState, useRef, useEffect, useCallback, use } from "react";
import { useRouter } from "next/navigation";
import { Icons } from "@/components/icons";
import { Badge, StatusBadge, Sparkline, Heatmap, Dropdown, Sheet, InlineEdit, HighlightedCEL, Kbd, Skeleton, fireConfetti, useToast } from "@/components/ui";
import { useApi, api } from "@/lib/api-client";
import { validatePolicyName, validateCEL } from "@/lib/validation";

const ACTION_COLOR: Record<string, string> = { block: "danger", steer: "warning", throttle: "warning", log: "muted", alert: "info", require_approval: "info" };

export default function PolicyDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const toast = useToast();

  const { data, loading, error, refetch } = useApi<{ data: any }>(`/api/policies/${id}`);
  const policy = data?.data || data;

  const [name, setName] = useState("");
  const [status, setStatus] = useState("enabled");
  const [priority, setPriority] = useState(100);
  const [action, setAction] = useState("block");
  const [cel, setCel] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [simRunning, setSimRunning] = useState(false);
  const [simResult, setSimResult] = useState<any>(null);
  const [diffVersion, setDiffVersion] = useState<any>(null);

  // Sync local state when policy loads
  useEffect(() => {
    if (policy) {
      setName(policy.name || "");
      setStatus(policy.status || "enabled");
      setPriority(policy.priority ?? 100);
      setAction(policy.action || "block");
      setCel(policy.cel || policy.expression || "");
    }
  }, [policy]);

  // Resizable split panel
  const [splitW, setSplitW] = useState(65);
  const splitRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);
  useEffect(() => {
    const onMove = (e: MouseEvent) => { if (!draggingRef.current || !splitRef.current) return; e.preventDefault(); const rect = splitRef.current.getBoundingClientRect(); setSplitW(Math.max(40, Math.min(80, ((e.clientX - rect.left) / rect.width) * 100))); };
    const onUp = () => { draggingRef.current = false; document.body.classList.remove("is-resizing"); };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => { window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); };
  }, []);

  async function handleSave() {
    const nameErr = validatePolicyName(name);
    if (nameErr) { toast.push({ tone: "danger", title: nameErr }); return; }
    const celErr = validateCEL(cel);
    if (celErr) { toast.push({ tone: "danger", title: celErr }); return; }
    setSaving(true);
    try {
      await api.patch(`/api/policies/${id}`, { name, status, action, priority, cel });
      setDirty(false);
      toast.push({ tone: "success", title: "Policy saved" });
      fireConfetti();
      refetch();
    } catch (e) {
      toast.push({ tone: "danger", title: "Save failed", body: e instanceof Error ? e.message : "Unknown error" });
    } finally {
      setSaving(false);
    }
  }

  async function runSimulation() {
    setSimRunning(true);
    setSimResult(null);
    try {
      const res = await api.post(`/api/policies/${id}`, { action: "simulate", cel });
      setSimResult(res?.data || res);
    } catch (e) {
      toast.push({ tone: "danger", title: "Simulation failed", body: e instanceof Error ? e.message : "Unknown error" });
    } finally {
      setSimRunning(false);
    }
  }

  if (loading) return <div className="page"><Skeleton width="100%" height={400} /></div>;
  if (error) return <div className="page"><div className="card" style={{ padding: 24, textAlign: "center" }}><div style={{ color: "var(--danger)", marginBottom: 8 }}>{error}</div><button className="btn" onClick={refetch}>Retry</button></div></div>;
  if (!policy) return <div className="page"><div className="card" style={{ padding: 24, textAlign: "center" }}>Policy not found.</div></div>;

  const versions = policy.versions || [];

  return (
    <div className="page" style={{ maxWidth: "none", padding: "20px 24px 48px" }}>
      <div className="policy-detail-header">
        <div className="policy-detail-title">
          <div className="t-caption text-muted" style={{ marginBottom: 4 }}>Policy</div>
          <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <h1 className="t-h2" style={{ fontFamily: "var(--font-mono)", margin: 0 }}>
              <InlineEdit value={name} onSave={(v) => { setName(v); setDirty(true); }} />
            </h1>
            {StatusBadge[status as keyof typeof StatusBadge]?.()}
            <Badge kind={ACTION_COLOR[action] || "muted"} mono>{action}</Badge>
          </div>
          <div className="t-sm text-secondary" style={{ marginTop: 4, maxWidth: 720 }}>{policy.description}</div>
        </div>
        <div className="policy-detail-actions">
          <button className="btn ghost" onClick={() => router.push("/policies")} disabled={saving}>Discard</button>
          <button className="btn primary" onClick={handleSave} disabled={saving}>
            {saving ? "Saving\u2026" : <><Icons.Save size={13} /> Save</>}
            {dirty && <span className="dot" style={{ background: "white", width: 6, height: 6, borderRadius: 999 }} />}
          </button>
        </div>
      </div>

      <div ref={splitRef} className="policy-edit-split" style={{ display: "grid", gridTemplateColumns: `${splitW}% 6px ${100 - splitW}%`, gap: 0, height: "calc(100vh - 200px)", minHeight: 540 }}>
        <div style={{ display: "flex", flexDirection: "column", minWidth: 0, border: "1px solid var(--border-subtle)", borderRadius: 8, overflow: "hidden", background: "var(--bg-surface)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 12px", borderBottom: "1px solid var(--border-subtle)", background: "var(--bg-input)" }}>
            <Icons.Code size={14} style={{ color: "var(--text-muted)" }} />
            <span className="t-sm" style={{ fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>policy.cel</span>
            <div style={{ flex: 1 }} />
            <span className="t-sm text-muted">CEL &middot; Common Expression Language</span>
          </div>
          <div style={{ flex: 1, overflow: "auto", padding: "12px 16px" }} onMouseDown={() => setDirty(true)}>
            <HighlightedCEL code={cel} />
          </div>
          <div style={{ borderTop: "1px solid var(--border-subtle)", padding: 16, background: "var(--bg)" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
              <Icons.Zap size={14} style={{ color: "var(--accent)" }} />
              <span style={{ fontWeight: 600, fontSize: 13 }}>Policy Impact Simulator</span>
              <div style={{ flex: 1 }} />
              <button className="btn sm" onClick={runSimulation} disabled={simRunning}>
                {simRunning ? <><span className="spinner" /> Running\u2026</> : <><Icons.Play size={12} /> Run against traces</>}
              </button>
            </div>
            {simResult && (
              <div>
                <div style={{ display: "flex", gap: 16, fontSize: 12.5, marginBottom: 10, flexWrap: "wrap" }}>
                  <span>Evaluated <b>{simResult.evaluated || 0}</b></span>
                  <span style={{ color: "var(--warning)" }}>Would flag <b>{simResult.would_flag || 0}</b></span>
                  <span style={{ color: "var(--danger)" }}>New blocks <b>+{simResult.new_blocks || 0}</b></span>
                </div>
                {simResult.examples?.length > 0 && (
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    {simResult.examples.map((ex: any, i: number) => (
                      <div key={i} style={{ display: "flex", alignItems: "center", gap: 10, padding: "6px 10px", borderRadius: 6, background: "var(--bg-input)", fontSize: 12.5 }}>
                        <span className="mono text-secondary">{ex.trace_id || ex.traceId}</span><span>{ex.agent}</span>
                        <span style={{ color: "var(--text-muted)", marginLeft: "auto" }}>{ex.reason}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
            {!simResult && !simRunning && <div className="t-sm text-muted" style={{ fontStyle: "italic" }}>Run the simulator to preview how this expression evaluates against historical traces.</div>}
          </div>
        </div>

        <div onMouseDown={(e) => { e.preventDefault(); draggingRef.current = true; document.body.classList.add("is-resizing"); }} style={{ cursor: "ew-resize", display: "grid", placeItems: "center", userSelect: "none" }}>
          <div style={{ width: 2, height: 36, background: "var(--border)", borderRadius: 2 }} />
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 14, overflowY: "auto", paddingLeft: 4 }}>
          <div className="card dense">
            <div className="card-header"><span className="card-title">Status</span></div>
            <div className="col" style={{ gap: 8 }}>
              {(["enabled", "shadow", "disabled"] as const).map((s) => (
                <label key={s} className="facet-item" style={{ cursor: "pointer", padding: "6px 8px", borderRadius: 6, background: status === s ? "var(--bg-active)" : "transparent" }} onClick={() => { setStatus(s); setDirty(true); }}>
                  <span style={{ width: 14, height: 14, borderRadius: 999, border: `1.5px solid ${status === s ? "var(--accent)" : "var(--border-emphasis)"}`, display: "grid", placeItems: "center" }}>
                    {status === s && <span style={{ width: 6, height: 6, borderRadius: 999, background: "var(--accent)" }} />}
                  </span>
                  <span style={{ flex: 1, textTransform: "capitalize" }}>{s}</span>
                </label>
              ))}
            </div>
          </div>
          <div className="card dense">
            <div className="card-header"><span className="card-title">Configuration</span></div>
            <div className="col" style={{ gap: 12 }}>
              <div>
                <div className="form-label">Action</div>
                <Dropdown width={220} trigger={({ toggle }) => (
                  <button className="btn" style={{ width: "100%", justifyContent: "space-between" }} onClick={toggle}>
                    <Badge kind={ACTION_COLOR[action] || "muted"} mono>{action}</Badge><Icons.ChevronDown size={13} />
                  </button>
                )} items={(["block", "steer", "throttle", "log", "alert", "require_approval"] as const).map((a) => ({ label: a, onClick: () => { setAction(a); setDirty(true); } }))} />
              </div>
              <div>
                <div className="form-label">Priority</div>
                <input className="input" type="number" value={priority} onChange={(e) => { setPriority(Number(e.target.value)); setDirty(true); }} />
              </div>
            </div>
          </div>
          {policy.hits7d && (
            <div className="card dense">
              <div className="card-header"><span className="card-title">Hits &middot; last 7 days</span><span className="card-subtle">{policy.hits7d.reduce((a: number, b: number) => a + b, 0)} total</span></div>
              <Sparkline data={policy.hits7d} width={300} height={64} color="var(--accent)" />
            </div>
          )}
          {versions.length > 0 && (
            <div className="card dense">
              <div className="card-header"><span className="card-title">Version history</span></div>
              <div className="col" style={{ gap: 8 }}>
                {versions.map((v: any) => (
                  <button key={v.v || v.version} onClick={() => setDiffVersion(v)} style={{ display: "flex", alignItems: "flex-start", gap: 10, padding: "8px 10px", borderRadius: 6, background: "var(--bg-input)", textAlign: "left", border: "1px solid transparent", cursor: "pointer" }}>
                    <Badge kind="muted" mono>v{v.v || v.version}</Badge>
                    <div style={{ flex: 1, minWidth: 0 }}><div className="t-sm" style={{ fontWeight: 500 }}>{v.note}</div><div className="t-sm text-muted">{v.when || v.created_at} &middot; {v.by || v.author}</div></div>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      <Sheet open={!!diffVersion} onClose={() => setDiffVersion(null)} eyebrow="Version diff" title={diffVersion ? `v${diffVersion.v || diffVersion.version}` : ""} wide
        footer={<><button className="btn ghost" onClick={() => setDiffVersion(null)}>Close</button><button className="btn" onClick={() => { setDiffVersion(null); toast.push({ tone: "warning", title: "Restored" }); }}><Icons.RotateCw size={13} /> Restore</button></>}>
        {diffVersion && <div className="code" style={{ fontSize: 12 }}><div className="t-sm text-secondary">{diffVersion.note}</div></div>}
      </Sheet>
    </div>
  );
}

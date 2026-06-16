"use client";

import { useState, useMemo, useRef, useEffect, useCallback } from "react";
import { Icons } from "@/components/icons";
import { useUser } from "@/lib/user-context";
import { Badge, Segmented, Checkbox, Switch, Dropdown, Sheet, useToast, Skeleton, Empty, Modal } from "@/components/ui";
import { useApi, api } from "@/lib/api-client";
import { setTheme as persistTheme, getStoredTheme } from "@/lib/theme";
import { usePermissions } from "@/lib/permissions";
import { formatDate, formatRelative } from "@/lib/format";


export function GeneralSettings() {
  const { user: currentUser, refetch } = useUser();
  const perms = usePermissions();
  const [confirmDeleteProject, setConfirmDeleteProject] = useState(false);
  const [deleteProjectText, setDeleteProjectText] = useState("");
  const [deletingProject, setDeletingProject] = useState(false);
  const { data: settingsData, loading: sLoading } = useApi<{ project_name?: string; project_slug?: string }>("/api/settings");
  const [name, setName] = useState("");
  const [avatarIdx, setAvatarIdx] = useState(0);
  const [projectName, setProjectName] = useState("");
  const [saving, setSaving] = useState(false);
  const toast = useToast();

  useEffect(() => { if (currentUser?.display_name) setName(currentUser.display_name); }, [currentUser]);
  useEffect(() => {
    if (settingsData) {
      if (settingsData.project_name) setProjectName(settingsData.project_name);
    }
  }, [settingsData]);
  useEffect(() => { try { const s = parseInt(localStorage.getItem("strathon-avatar-idx") || "0", 10); if (s >= 0 && s < 7) setAvatarIdx(s); } catch {} }, []);
  const [theme, setTheme] = useState<string>("system");
  const [isDark, setIsDark] = useState(true);
  useEffect(() => { setTheme(getStoredTheme()); setIsDark(document.documentElement.dataset.theme !== "light"); }, []);

  const AVATARS = [
    { bg: isDark ? "#2a2a2a" : "#e8e6e1", icon: null as React.ReactNode },
    { bg: "#D4819C", icon: <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3l1.9 5.8a2 2 0 001.3 1.3L21 12l-5.8 1.9a2 2 0 00-1.3 1.3L12 21l-1.9-5.8a2 2 0 00-1.3-1.3L3 12l5.8-1.9a2 2 0 001.3-1.3z" /></svg> },
    { bg: "#7BC67E", icon: <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 3a3 3 0 00-3 3v12a3 3 0 003 3 3 3 0 003-3 3 3 0 00-3-3H6a3 3 0 00-3 3 3 3 0 003 3 3 3 0 003-3V6a3 3 0 00-3-3 3 3 0 00-3 3 3 3 0 003 3h12a3 3 0 003-3 3 3 0 00-3-3z" /></svg> },
    { bg: "#E8A849", icon: <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2L2 7l10 5 10-5-10-5z" /><path d="M2 17l10 5 10-5" /><path d="M2 12l10 5 10-5" /></svg> },
    { bg: "#DB7B7B", icon: <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 3v4M3 5h4M6 17v4M4 19h4M13 3l2 2M19.5 8.5l.5.5M17 17l2 2M14 14l7-7" /><path d="M9.5 9.5L3 16v5h5l6.5-6.5" /></svg> },
    { bg: "#5BA8C8", icon: <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M17.5 19H9a7 7 0 110-14h8.5" /><polyline points="21 12 17 16" /><polyline points="21 12 17 8" /></svg> },
    { bg: "#B8856B", icon: <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="4" y1="21" x2="4" y2="14" /><line x1="4" y1="10" x2="4" y2="3" /><line x1="12" y1="21" x2="12" y2="12" /><line x1="12" y1="8" x2="12" y2="3" /><line x1="20" y1="21" x2="20" y2="16" /><line x1="20" y1="12" x2="20" y2="3" /><line x1="1" y1="14" x2="7" y2="14" /><line x1="9" y1="8" x2="15" y2="8" /><line x1="17" y1="16" x2="23" y2="16" /></svg> },
  ];
  const cycleAvatar = () => { const next = (avatarIdx + 1) % AVATARS.length; setAvatarIdx(next); localStorage.setItem("strathon-avatar-idx", String(next)); window.dispatchEvent(new CustomEvent("avatar-changed", { detail: next })); };
  const av = AVATARS[avatarIdx];

  const saveSettings = async () => {
    setSaving(true);
    try {
      await api.patch("/api/settings", { project_name: projectName });
      const trimmedName = name.trim();
      if (trimmedName && trimmedName !== currentUser?.display_name) {
        await api.patch("/api/auth/me", { display_name: trimmedName });
      }
      // Refresh user context so the new name and project name propagate to
      // the project switcher, members list, and avatar menu immediately.
      refetch();
      toast.push({ tone: "success", title: "Settings saved" });
    } catch (err) {
      toast.push({ tone: "danger", title: err instanceof Error ? err.message : "Failed to save" });
    } finally {
      setSaving(false);
    }
  };

  const deleteProject = async () => {
    const slug = settingsData?.project_slug;
    if (!slug) { toast.push({ tone: "danger", title: "Could not determine the project to delete" }); return; }
    setDeletingProject(true);
    try {
      await api.del(`/api/projects/${encodeURIComponent(slug)}`);
      toast.push({ tone: "success", title: "Project deleted" });
      // The BFF cleared the project cookie; a full reload re-heals to a
      // remaining project (or shows the no-project gate).
      window.location.href = "/";
    } catch (err) {
      // The receiver returns 409 when this is the last project.
      toast.push({ tone: "danger", title: err instanceof Error ? err.message : "Failed to delete project" });
      setDeletingProject(false);
    }
  };

  const Row = ({ label, sub, children }: { label: string; sub?: string; children: React.ReactNode }) => (
    <div className="settings-row">
      <div className="settings-row-label"><span>{label}</span>{sub && <span className="t-sm text-muted">{sub}</span>}</div>
      <div className="settings-row-value">{children}</div>
    </div>
  );

  return (
    <div className="settings-rows">
      <div className="settings-section-title">Profile</div>
      <Row label="Avatar">
        <button className="settings-avatar-btn" title="Click to shuffle avatar" onClick={cycleAvatar} style={{ background: av.bg, color: avatarIdx === 0 && !isDark ? "#3a3830" : "rgba(255,255,255,0.85)", borderColor: avatarIdx === 0 && !isDark ? "rgba(0,0,0,0.12)" : "rgba(255,255,255,0.12)" }}>
          {av.icon ? av.icon : <span>{name.charAt(0).toUpperCase()}</span>}
          <span className="shuffle-icon"><Icons.Shuffle size={16} /></span>
        </button>
      </Row>
      <Row label="Full name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" /></Row>
      <Row label="Email"><span className="text-secondary" style={{ fontSize: 13.5 }}>{currentUser?.email || ""}</span></Row>

      <div className="settings-section-title" style={{ marginTop: 8 }}>Preferences</div>
      <Row label="Appearance">
        <div className="theme-picker">
          {[{ v: "dark", Icon: Icons.Moon }, { v: "light", Icon: Icons.Sun }, { v: "system", Icon: Icons.Cpu }].map((opt) => (
            <button key={opt.v} className="theme-opt" data-active={theme === opt.v} onClick={() => { persistTheme(opt.v as "light" | "dark" | "system"); setTheme(opt.v); setIsDark(document.documentElement.dataset.theme !== "light"); }} title={opt.v}>
              <opt.Icon size={14} />
            </button>
          ))}
        </div>
      </Row>
      <Row label="Timezone">
        <span className="text-secondary" style={{ fontSize: 14 }}>UTC</span>
      </Row>
      <Row label="Language">
        <span className="text-secondary" style={{ fontSize: 14 }}>English (US)</span>
      </Row>

      <div className="settings-section-title" style={{ marginTop: 8 }}>Workspace</div>
      <Row label="Project name">
        {sLoading ? <Skeleton width={200} height={32} /> : (
          <input className="input" value={projectName} onChange={(e) => setProjectName(e.target.value)} style={{ maxWidth: 240 }} />
        )}
      </Row>
      <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 12, gap: 8 }}>
        <button className="btn primary" onClick={saveSettings} disabled={saving}>
          {saving ? <><span className="spinner" /> Saving&hellip;</> : "Save changes"}
        </button>
      </div>

      {perms.isOwner && (
        <>
          <div className="settings-section-title" style={{ marginTop: 24 }}>Danger zone</div>
          <div style={{ border: "1px solid color-mix(in oklab, var(--danger) 38%, transparent)", borderRadius: 8, overflow: "hidden", background: "var(--bg-deep)" }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "16px 18px", gap: 16 }}>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div style={{ fontWeight: 600, fontSize: 13.5, marginBottom: 2 }}>Delete this project</div>
                <div className="t-sm text-secondary">Permanently removes this project and all its policies, traces, and audit data. This cannot be undone. You must have another project to switch to.</div>
              </div>
              <button className="btn danger" style={{ flexShrink: 0 }} onClick={() => { setConfirmDeleteProject(true); setDeleteProjectText(""); }}>Delete project</button>
            </div>
          </div>
        </>
      )}

      <Modal open={confirmDeleteProject} onClose={() => { setConfirmDeleteProject(false); setDeleteProjectText(""); }} danger
        title="Delete this project?" confirmLabel={deletingProject ? "Deleting…" : "Delete project"}
        onConfirm={() => { if (deleteProjectText === (settingsData?.project_name || "")) deleteProject(); else toast.push({ tone: "danger", title: "Project name doesn't match" }); }}
        body={
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <span>This permanently deletes <b>{settingsData?.project_name || "this project"}</b> and all its data. This cannot be undone.</span>
            <span className="t-sm text-muted">Type <b>{settingsData?.project_name}</b> to confirm:</span>
            <input className="input" value={deleteProjectText} onChange={(e) => setDeleteProjectText(e.target.value)} placeholder={settingsData?.project_name || ""} />
          </div>
        } />
    </div>
  );
}


export function RetentionSliders() {
  const { data: settingsData, loading } = useApi<{ retention?: { traces_days?: number; audit_days?: number; spans_days?: number } }>("/api/settings");
  const [values, setValues] = useState<Record<string, number>>({ traces: 30, audit: 365, spans: 14 });
  const [saving, setSaving] = useState(false);
  const toast = useToast();

  useEffect(() => {
    if (settingsData?.retention) {
      setValues({
        traces: settingsData.retention.traces_days || 30,
        audit: settingsData.retention.audit_days || 365,
        spans: settingsData.retention.spans_days || 14,
      });
    }
  }, [settingsData]);

  const saveRetention = async () => {
    setSaving(true);
    try {
      await api.patch("/api/settings", { retention: { traces_days: values.traces, audit_days: values.audit, spans_days: values.spans } });
      toast.push({ tone: "success", title: "Retention settings saved" });
    } catch (err) {
      toast.push({ tone: "danger", title: err instanceof Error ? err.message : "Failed" });
    } finally {
      setSaving(false);
    }
  };

  const slider = (key: string, label: string, min: number, max: number, unit = "days") => {
    const v = values[key];
    return (
      <div className="card dense">
        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
          <span style={{ fontWeight: 500 }}>{label}</span>
          <span className="mono" style={{ color: "var(--accent)" }}>{v} {unit}</span>
        </div>
        <input type="range" min={min} max={max} value={v} onChange={(e) => setValues({ ...values, [key]: Number(e.target.value) })} style={{ width: "100%", accentColor: "var(--accent)" }} />
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "var(--text-muted)" }}><span>{min} {unit}</span><span>{max} {unit}</span></div>
      </div>
    );
  };

  if (loading) return <Skeleton width="100%" height={200} />;

  return (
    <>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 12 }}>
        {slider("traces", "Trace data", 7, 365)}{slider("spans", "Span payloads", 1, 90)}{slider("audit", "Audit log (compliance)", 30, 2555)}
      </div>
      <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 12 }}>
        <button className="btn primary" onClick={saveRetention} disabled={saving}>
          {saving ? <><span className="spinner" /> Saving&hellip;</> : "Save retention settings"}
        </button>
      </div>
    </>
  );
}


export function ApiKeysSection() {
  const perms = usePermissions();
  const { data: keysData, loading, refetch } = useApi<{ data: any[] }>("/api/api-keys");
  const [createOpen, setCreateOpen] = useState(false);
  const [createdSecret, setCreatedSecret] = useState<string | null>(null);
  const [secretSaved, setSecretSaved] = useState(false);
  const [creating, setCreating] = useState(false);
  const [keyName, setKeyName] = useState("");
  const toast = useToast();

  const keys = keysData?.data || [];

  const createKey = async () => {
    if (!keyName.trim()) { toast.push({ tone: "warning", title: "Name is required" }); return; }
    setCreating(true);
    try {
      const res = await api.post("/api/api-keys", { name: keyName.trim() });
      setCreatedSecret(res.key || res.data?.key || res.secret);
      refetch();
    } catch (err) {
      toast.push({ tone: "danger", title: err instanceof Error ? err.message : "Failed" });
    } finally {
      setCreating(false);
    }
  };

  const revokeKey = async (id: string) => {
    try {
      await api.del(`/api/api-keys?id=${id}`);
      toast.push({ tone: "success", title: "Key revoked" });
      refetch();
    } catch (err) {
      toast.push({ tone: "danger", title: err instanceof Error ? err.message : "Failed" });
    }
  };

  const rotateKey = async (id: string) => {
    try {
      const res = await api.post(`/api/api-keys/${id}/rotate`);
      setCreatedSecret(res.key || res.data?.key || res.secret);
      setCreateOpen(true);
      setSecretSaved(false);
      toast.push({ tone: "success", title: "Key rotated", body: "The old key is now invalid." });
      refetch();
    } catch (err) {
      toast.push({ tone: "danger", title: err instanceof Error ? err.message : "Failed" });
    }
  };

  return (
    <div className="apikeys-section">
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 24, gap: 16 }}>
        <div>
          <h2 className="t-h2" style={{ marginBottom: 4 }}>API keys</h2>
          <p className="text-secondary">Used by the SDK and CLI to authenticate with the receiver.</p>
        </div>
        {perms.canManageApiKeys && <button className="btn primary" style={{ flexShrink: 0 }} onClick={() => { setCreateOpen(true); setKeyName(""); setCreatedSecret(null); setSecretSaved(false); }}><Icons.Plus size={13} /> Create key</button>}
      </div>

      {loading ? <Skeleton width="100%" height={200} /> : keys.length === 0 ? (
        <Empty icon={<Icons.Key size={24} />} title="No API keys yet"
          subtitle="Create a key to connect agents to this workspace." />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Name</th><th>Key prefix</th><th>Created</th><th>Last used</th><th /></tr></thead>
            <tbody>
              {keys.map((k: any) => (
                <tr key={k.id}>
                  <td style={{ fontWeight: 500 }}>{k.name}</td>
                  <td className="mono" style={{ fontSize: 12.5 }}>{k.prefix || k.key_prefix || "sk_…"}</td>
                  <td className="text-secondary">{k.created_at || k.created ? formatDate(k.created_at || k.created) : ""}</td>
                  <td className="text-secondary">{k.last_used_at || k.last_used ? formatRelative(k.last_used_at || k.last_used) : "never"}</td>
                  <td>
                    <Dropdown align="right" width={160}
                      trigger={({ toggle }) => <button className="btn icon ghost sm" onClick={toggle}><Icons.MoreHorizontal size={14} /></button>}
                      items={[
                        { icon: <Icons.RotateCw size={13} />, label: "Rotate", onClick: () => rotateKey(k.id) },
                        { divider: true },
                        { icon: <Icons.Trash size={13} />, label: "Revoke", danger: true, onClick: () => revokeKey(k.id) },
                      ]} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Sheet open={createOpen} onClose={() => { setCreateOpen(false); setCreatedSecret(null); setSecretSaved(false); }} eyebrow="API keys" title={createdSecret ? "Save this secret" : "Create a new API key"}
        footer={createdSecret ? (
          <>
            <span className="t-sm text-muted" style={{ marginRight: "auto", display: "flex", alignItems: "center", gap: 6 }}><Checkbox checked={secretSaved} onChange={setSecretSaved} /> I&apos;ve saved this secret</span>
            <button className="btn primary" disabled={!secretSaved} onClick={() => { setCreateOpen(false); setCreatedSecret(null); setSecretSaved(false); }}>Done</button>
          </>
        ) : (
          <>
            <button className="btn ghost" onClick={() => setCreateOpen(false)}>Cancel</button>
            <button className="btn primary" onClick={createKey} disabled={creating}>{creating ? "Creating\u2026" : "Create key"}</button>
          </>
        )}>
        {!createdSecret ? (
          <div className="col" style={{ gap: 16 }}>
            <div><div className="form-label">Name</div><input className="input" value={keyName} onChange={(e) => setKeyName(e.target.value)} placeholder="e.g. prod-receiver" autoFocus onKeyDown={(e) => { if (e.key === "Enter" && keyName.trim()) { e.preventDefault(); createKey(); } }} /></div>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button className="btn ghost" onClick={() => setCreateOpen(false)}>Cancel</button>
              <button className="btn primary" onClick={createKey} disabled={creating || !keyName.trim()}>{creating ? "Creating\u2026" : "Create key"}</button>
            </div>
          </div>
        ) : (
          <div>
            <div className="card dense" style={{ background: "var(--warning-bg)", borderColor: "var(--warning-border)", marginBottom: 16 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}><Icons.AlertTriangle size={14} stroke="var(--warning)" /><span style={{ fontWeight: 600 }}>This secret will not be shown again.</span></div>
              <div className="t-sm text-secondary" style={{ marginTop: 6 }}>Copy it now and store it in a secret manager. If you lose it, revoke this key and create a new one.</div>
            </div>
            <div style={{ position: "relative" }}>
              <div className="code" style={{ wordBreak: "break-all", whiteSpace: "pre-wrap", padding: "14px 16px", paddingRight: 50 }}>{createdSecret}</div>
              <button className="btn icon ghost" style={{ position: "absolute", top: 8, right: 8 }} onClick={() => { navigator.clipboard?.writeText(createdSecret); toast.push({ tone: "success", title: "Copied to clipboard" }); }}><Icons.Copy size={14} /></button>
            </div>
          </div>
        )}
      </Sheet>
    </div>
  );
}


export function ExportSection() {
  const toast = useToast();
  const DATASETS = [
    { id: "policies", label: "Policies", icon: "Shield", desc: "Definitions, CEL, version history" },
    { id: "traces", label: "Traces", icon: "GitBranch", desc: "Root traces with span counts and outcomes" },
    { id: "spans", label: "Spans", icon: "Search", desc: "Per-operation span data (large)" },
    { id: "approvals", label: "Approval history", icon: "UserCheck", desc: "Pending + resolved human-in-the-loop decisions" },
    { id: "agents", label: "Agents", icon: "Bot", desc: "Deployed agent configurations + risk scores" },
    { id: "audit", label: "Audit log", icon: "ScrollText", desc: "Append-only signed governance event log" },
    { id: "budgets", label: "Budget series", icon: "Dollar", desc: "Daily spend by agent, last 30 days" },
    { id: "compliance", label: "Compliance evidence", icon: "FileCheck", desc: "Framework coverage + evidence pack" },
  ];
  const [selected, setSelected] = useState<Set<string>>(new Set(["policies", "traces", "audit"]));
  const [time, setTime] = useState("7d");
  const [format, setFormat] = useState("json");
  const [generating, setGenerating] = useState(false);
  const toggle = (id: string) => setSelected((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });

  const generate = async () => {
    if (selected.size === 0) { toast.push({ tone: "warning", title: "Select at least one dataset" }); return; }
    setGenerating(true);
    try {
      const res = await fetch("/api/export", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ datasets: [...selected], time_range: time, format }),
      });
      if (res.ok) {
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        // CSV exports are delivered as a ZIP of per-dataset CSVs.
        const ext = format === "csv" ? "zip" : "json";
        a.href = url; a.download = `strathon-export-${Date.now()}.${ext}`; a.click();
        URL.revokeObjectURL(url);
        toast.push({ tone: "success", title: "Export downloaded" });
      } else {
        toast.push({ tone: "danger", title: "Export failed" });
      }
    } catch {
      toast.push({ tone: "danger", title: "Export failed" });
    } finally {
      setGenerating(false);
    }
  };

  return (
    <>
      <h2 className="t-h2" style={{ marginBottom: 4 }}>Export data</h2>
      <p className="text-secondary" style={{ marginBottom: 24 }}>Generate compliance-grade snapshots of any data in this workspace.</p>
      <div className="card">
        <div className="card-header">
          <span className="card-title">Select datasets</span>
          <div style={{ display: "flex", gap: 6 }}>
            <button className="btn ghost sm" onClick={() => setSelected(new Set(DATASETS.map((d) => d.id)))}>All</button>
            <button className="btn ghost sm" onClick={() => setSelected(new Set())}>None</button>
          </div>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(290px, 1fr))", gap: 8, marginTop: 4 }}>
          {DATASETS.map((d) => {
            const Icon = Icons[d.icon as keyof typeof Icons] || Icons.Hash;
            const isOn = selected.has(d.id);
            return (
              <button key={d.id} onClick={() => toggle(d.id)} style={{ display: "flex", alignItems: "flex-start", gap: 10, padding: "12px 12px", borderRadius: 8, border: `1px solid ${isOn ? "var(--accent-border)" : "var(--border-subtle)"}`, background: isOn ? "var(--accent-bg)" : "var(--bg-surface)", textAlign: "left", cursor: "pointer", transition: "background 120ms, border-color 120ms" }}>
                <Checkbox checked={isOn} onChange={() => toggle(d.id)} />
                <div style={{ width: 28, height: 28, borderRadius: 6, background: isOn ? "color-mix(in oklab, var(--accent) 18%, transparent)" : "var(--bg-input)", color: isOn ? "var(--accent)" : "var(--text-secondary)", display: "grid", placeItems: "center", flexShrink: 0 }}><Icon size={14} /></div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <span style={{ fontWeight: 600, fontSize: 13.5, lineHeight: 1.3 }}>{d.label}</span>
                  <div className="t-sm text-muted" style={{ marginTop: 2, lineHeight: 1.35 }}>{d.desc}</div>
                </div>
              </button>
            );
          })}
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-header"><span className="card-title">Options</span></div>
        <div className="col" style={{ gap: 16 }}>
          <div><div className="form-label">Time range</div><Segmented value={time} onChange={setTime} options={[{ label: "24h", value: "24h" }, { label: "7d", value: "7d" }, { label: "30d", value: "30d" }, { label: "90d", value: "90d" }, { label: "1y", value: "1y" }]} /></div>
          <div><div className="form-label">Format</div><Segmented value={format} onChange={setFormat} options={[{ label: "JSON", value: "json" }, { label: "CSV", value: "csv" }]} /></div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16, display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap", position: "sticky", bottom: 12, background: "var(--bg-elevated)", boxShadow: "var(--shadow-md)", zIndex: 5 }}>
        <div style={{ flex: 1, minWidth: 200 }}>
          <div className="t-sm text-secondary">
            <strong style={{ color: "var(--text)", fontVariantNumeric: "tabular-nums" }}>{selected.size}</strong> {selected.size === 1 ? "dataset" : "datasets"} &middot; {format.toUpperCase()} &middot; last {time}
          </div>
        </div>
        <button className="btn primary" disabled={generating || selected.size === 0} onClick={generate}>{generating ? <><span className="spinner" /> Generating&hellip;</> : <><Icons.Download size={13} /> Generate export</>}</button>
      </div>
    </>
  );
}


const CHANNEL_TYPES = [
  { id: "slack", label: "Slack", configKey: "webhook_url", configLabel: "Incoming webhook URL", placeholder: "https://hooks.slack.com/services/..." },
  { id: "discord", label: "Discord", configKey: "webhook_url", configLabel: "Webhook URL", placeholder: "https://discord.com/api/webhooks/..." },
  { id: "webhook", label: "Webhook", configKey: "url", configLabel: "Endpoint URL", placeholder: "https://example.com/hooks/strathon" },
  { id: "github", label: "GitHub", configKey: "repo", configLabel: "Repository (owner/repo)", placeholder: "your-org/your-repo" },
] as const;

const CHANNEL_EVENTS = [
  "approval_request", "incident", "policy_blocked", "policy_steered",
  "policy_throttled", "policy_alert", "budget_alert", "budget_halt",
  "heartbeat_missed", "behavioral_drift", "sdk_integrity_violation",
];

// High-signal events we suggest by default: the "something important happened"
// set. The steered/throttled/alert events can be chatty on busy agents, so they
// are opt-in rather than recommended.
const RECOMMENDED_EVENTS = new Set([
  "approval_request", "incident", "policy_blocked", "budget_halt",
  "behavioral_drift", "sdk_integrity_violation",
]);

export function IntegrationsSection() {
  const perms = usePermissions();
  const { data, loading, refetch } = useApi<{ data: any[] }>("/api/notifications");
  const [createOpen, setCreateOpen] = useState(false);
  const [chType, setChType] = useState<string>("slack");
  const [chName, setChName] = useState("");
  const [chConfig, setChConfig] = useState("");
  const [chToken, setChToken] = useState("");
  const [chEvents, setChEvents] = useState<string[]>(() => CHANNEL_EVENTS.filter((e) => RECOMMENDED_EVENTS.has(e)));
  const [saving, setSaving] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);
  const toast = useToast();

  const channels = data?.data || [];
  const typeMeta = CHANNEL_TYPES.find((t) => t.id === chType) || CHANNEL_TYPES[0];

  const resetForm = () => { setChType("slack"); setChName(""); setChConfig(""); setChToken(""); setChEvents(CHANNEL_EVENTS.filter((e) => RECOMMENDED_EVENTS.has(e))); };

  const createChannel = async () => {
    if (!chName.trim()) { toast.push({ tone: "warning", title: "Name is required" }); return; }
    if (!chConfig.trim()) { toast.push({ tone: "warning", title: `${typeMeta.configLabel} is required` }); return; }
    if (chType === "github" && !chToken.trim()) { toast.push({ tone: "warning", title: "GitHub token is required" }); return; }
    if (chEvents.length === 0) { toast.push({ tone: "warning", title: "Select at least one event" }); return; }
    setSaving(true);
    try {
      const config: Record<string, string> = { [typeMeta.configKey]: chConfig.trim() };
      if (chType === "github") config.token = chToken.trim();
      await api.post("/api/notifications", {
        channel_type: chType,
        name: chName.trim(),
        config,
        events: chEvents,
        enabled: true,
      });
      toast.push({ tone: "success", title: "Integration added" });
      setCreateOpen(false);
      resetForm();
      refetch();
    } catch (err) {
      toast.push({ tone: "danger", title: err instanceof Error ? err.message : "Failed to add" });
    } finally {
      setSaving(false);
    }
  };

  const toggleEnabled = async (ch: any) => {
    try {
      await api.patch(`/api/notifications/${ch.id}`, { enabled: !ch.enabled });
      refetch();
    } catch (err) {
      toast.push({ tone: "danger", title: err instanceof Error ? err.message : "Failed" });
    }
  };

  const deleteChannel = async (id: string) => {
    try {
      await api.del(`/api/notifications/${id}`);
      toast.push({ tone: "success", title: "Integration removed" });
      setDeleteTarget(null);
      refetch();
    } catch (err) {
      toast.push({ tone: "danger", title: err instanceof Error ? err.message : "Failed" });
    }
  };

  return (
    <>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 24 }}>
        <div>
          <h2 className="t-h2" style={{ marginBottom: 4 }}>Integrations</h2>
          <p className="text-secondary">Send approvals, incidents, and policy alerts to Slack, Discord, GitHub, or a webhook.</p>
        </div>
        {perms.canEditSettings && <button className="btn primary" onClick={() => { resetForm(); setCreateOpen(true); }}><Icons.Plus size={13} /> Add integration</button>}
      </div>

      {loading ? <Skeleton width="100%" height={120} /> : channels.length === 0 ? (
        <Empty icon={<Icons.Bell size={24} />} title="No integrations yet" subtitle="Add a Slack, Discord, GitHub, or webhook channel to receive alerts." />
      ) : (
        <div className="col" style={{ gap: 10 }}>
          {channels.map((ch: any) => {
            const meta = CHANNEL_TYPES.find((t) => t.id === ch.channel_type);
            return (
              <div key={ch.id} className="card" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16 }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ fontWeight: 600 }}>{ch.name}</span>
                    <Badge kind="muted">{meta?.label || ch.channel_type}</Badge>
                    {!ch.enabled && <Badge kind="muted">Disabled</Badge>}
                  </div>
                  <div className="t-sm text-secondary" style={{ marginTop: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {(ch.events || []).length} event{(ch.events || []).length === 1 ? "" : "s"}
                  </div>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 12, flexShrink: 0 }}>
                  {perms.canEditSettings && <Switch on={!!ch.enabled} onChange={() => toggleEnabled(ch)} />}
                  {perms.canEditSettings && <button className="btn icon ghost" onClick={() => setDeleteTarget({ id: ch.id, name: ch.name })}><Icons.Trash size={14} /></button>}
                </div>
              </div>
            );
          })}
        </div>
      )}

      <Sheet open={createOpen} onClose={() => setCreateOpen(false)} eyebrow="Integrations" title="Add integration">
        <div className="col" style={{ gap: 14 }}>
          <div>
            <div className="form-label">Type</div>
            <Segmented value={chType} onChange={setChType} options={CHANNEL_TYPES.map((t) => ({ label: t.label, value: t.id }))} />
          </div>
          <div>
            <div className="form-label">Name</div>
            <input className="input" value={chName} onChange={(e) => setChName(e.target.value)} placeholder="e.g. Engineering alerts" />
          </div>
          <div>
            <div className="form-label">{typeMeta.configLabel}</div>
            <input className="input" value={chConfig} onChange={(e) => setChConfig(e.target.value)} placeholder={typeMeta.placeholder} />
          </div>
          {chType === "github" && (
            <div>
              <div className="form-label">GitHub token</div>
              <input className="input" type="password" value={chToken} onChange={(e) => setChToken(e.target.value)} placeholder="ghp_..." autoComplete="off" />
            </div>
          )}
          <div>
            <div className="form-label">Events</div>
            <div className="t-sm text-secondary" style={{ marginTop: -4, marginBottom: 8 }}>
              The recommended set covers the important alerts without noise. Steered, throttled, and alert events can be chatty on busy agents.
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {CHANNEL_EVENTS.map((ev) => {
                const on = chEvents.includes(ev);
                const rec = RECOMMENDED_EVENTS.has(ev);
                return (
                  <button key={ev} type="button" className="chip" data-active={on}
                    title={rec ? "Recommended" : undefined}
                    onClick={() => setChEvents((prev) => on ? prev.filter((x) => x !== ev) : [...prev, ev])}>
                    {ev.replace(/_/g, " ")}{rec && <span style={{ marginLeft: 5, color: "var(--accent)", fontWeight: 600 }}>· rec</span>}
                  </button>
                );
              })}
            </div>
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 4, paddingTop: 16, borderTop: "1px solid var(--border-subtle)" }}>
            <button className="btn ghost" onClick={() => setCreateOpen(false)}>Cancel</button>
            <button className="btn primary" onClick={createChannel} disabled={saving}>{saving ? <><span className="spinner" /> Adding&hellip;</> : "Add integration"}</button>
          </div>
        </div>
      </Sheet>

      <Modal open={!!deleteTarget} onClose={() => setDeleteTarget(null)} title="Remove integration?" danger confirmLabel="Remove"
        body={<>Remove <strong>{deleteTarget?.name}</strong>? Alerts will no longer be sent to this channel.</>}
        onConfirm={() => { if (deleteTarget) deleteChannel(deleteTarget.id); }} />
    </>
  );
}

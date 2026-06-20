"use client";
import { useState, useRef, useEffect } from "react";
import { Icons } from "@/components/icons";
import { Badge, Segmented, Sheet, SkeletonTable, Time } from "@/components/ui";
import { useApi, api } from "@/lib/api-client";

export default function AuditPage() {
  const [q, setQ] = useState("");
  const [time, setTime] = useState("7d");
  const [popover, setPopover] = useState<string | null>(null);
  const [diff, setDiff] = useState<any>(null);
  const popRef = useRef<HTMLDivElement>(null);

  useEffect(() => { const onDoc = (e: MouseEvent) => { if (popRef.current && !popRef.current.contains(e.target as Node)) setPopover(null); }; document.addEventListener("mousedown", onDoc); return () => document.removeEventListener("mousedown", onDoc); }, []);

  const params: Record<string, string> = { range: time };
  if (q) params.search = q;
  const { data, loading, error, refetch } = useApi<{ data: any[] }>("/api/audit", params, [q, time]);
  const entries = data?.data || [];
  const editable = (a: string) => a.includes("update") || a.includes("create") || a.includes("delete") || a.includes("revoke");

  if (error) return <div className="page"><div className="card" style={{ padding: 24, textAlign: "center" }}><div style={{ color: "var(--danger)", marginBottom: 8 }}>{error}</div><button className="btn" onClick={refetch}>Retry</button></div></div>;

  function handleExport() {
    if (!entries.length) { return; }
    const cols = ["timestamp", "actor", "category", "action", "resource", "outcome"];
    const cell = (v: unknown) => {
      const s = v == null ? "" : String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const rows = entries.map((e: any) => [
      e.ts ?? e.timestamp ?? "",
      e.actor ?? "",
      e.category ?? "",
      e.action ?? "",
      e.resource ?? "",
      e.outcome ?? e.status ?? "",
    ].map(cell).join(","));
    const csv = [cols.join(","), ...rows].join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "strathon-audit.csv"; a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="page">
      <div className="page-header">
        <div><h1 className="t-h1 page-title">Audit log</h1>
          <div className="page-subtitle">Tamper-evident. Every entry is hash-chained and anchored hourly.
            <span style={{ marginLeft: 8, display: "inline-flex", alignItems: "center", gap: 4, color: "var(--success)" }}><Icons.ShieldCheck size={13} /> chain verified</span>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn ghost" onClick={handleExport}><Icons.Download size={13} /> Export CSV</button>
          <Segmented value={time} onChange={setTime} options={["24h", "7d", "30d", "90d"].map((v) => ({ label: v, value: v }))} />
        </div>
      </div>
      <div className="table-toolbar"><div className="input-wrap" style={{ width: 380 }}><Icons.Search size={14} /><input className="input search" placeholder="actor:user@example.com  category:policy  …" value={q} onChange={(e) => setQ(e.target.value)} /></div><div className="grow" /><span className="t-sm text-muted">{entries.length} entries</span></div>
      <div className="table-wrap">
        {loading ? <SkeletonTable rows={8} columns={[1, 2, 1, 1, 1, 1, 1]} /> : entries.length === 0 ? (
          <div style={{ padding: "48px 24px", textAlign: "center" }}>
            <Icons.ScrollText size={32} style={{ color: "var(--text-muted)", marginBottom: 8 }} />
            <div style={{ fontWeight: 600 }}>Audit log is empty</div>
            <div className="t-sm text-muted" style={{ marginTop: 4 }}>Events will appear here as users and agents interact with the system.</div>
          </div>
        ) : (
          <table className="table">
            <thead><tr><th style={{ width: 36 }} /><th style={{ width: 200 }}>Timestamp</th><th>Action</th><th>Category</th><th>Actor</th><th>Resource</th><th>IP</th></tr></thead>
            <tbody>
              {entries.map((e: any) => (
                <tr key={e.id} className={editable(e.action) ? "clickable" : ""} onClick={() => { if (editable(e.action)) setDiff(e); }}>
                  <td style={{ position: "relative" }}>
                    <button title={e.verified ? "Chain verified" : "Not yet anchored"} onClick={(ev) => { ev.stopPropagation(); setPopover(e.id); }}
                      style={{ width: 22, height: 22, borderRadius: 5, display: "grid", placeItems: "center", background: e.verified ? "var(--success-bg)" : "var(--warning-bg)", color: e.verified ? "var(--success)" : "var(--warning)" }}>
                      {e.verified ? <Icons.Lock size={11} /> : <Icons.Clock size={11} />}
                    </button>
                    {popover === e.id && (
                      <div ref={popRef} style={{ position: "absolute", marginTop: 6, zIndex: 30, background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: 10, boxShadow: "var(--shadow-lg)", padding: 14, width: 360, animation: "scale-fade 120ms ease-out" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}><Icons.ShieldCheck size={14} stroke={e.verified ? "var(--success)" : "var(--warning)"} /><span style={{ fontWeight: 600 }}>{e.verified ? "Chain verified" : "Pending anchor"}</span></div>
                        <div className="t-sm text-secondary" style={{ marginBottom: 10 }}>{e.verified ? "This entry\u2019s hash chain links back to an anchored Merkle root." : "Entry committed but not yet anchored (runs hourly)."}</div>
                        {e.hash && <><div className="t-caption text-muted" style={{ marginBottom: 4 }}>Entry hash</div><div className="code" style={{ wordBreak: "break-all", fontSize: 11, padding: 8, whiteSpace: "pre-wrap" }}>{e.hash}</div></>}
                      </div>
                    )}
                  </td>
                  <td className="mono text-secondary" style={{ fontSize: 12 }}><Time absolute ago={e.ts || e.timestamp} /></td>
                  <td><Badge kind={e.action.includes("delete") || e.action.includes("revoke") || e.action.includes("deny") ? "danger" : e.action.includes("create") ? "success" : "muted"} mono>{e.action}</Badge></td>
                  <td className="text-secondary">{e.category}</td>
                  <td>
                    {/* Visually distinguish API-key actors from human users and
                        system services. The transform stamps actor_type from the
                        receiver (user|api_key|system|service). */}
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      {e.actor_type === "user"
                        ? <Icons.User size={12} style={{ color: "var(--text-muted)" }} />
                        : e.actor_type === "api_key"
                        ? <Icons.Key size={12} style={{ color: "var(--text-muted)" }} />
                        : <Icons.Terminal size={12} style={{ color: "var(--text-muted)" }} />}
                      <span>{e.actor || "—"}</span>
                    </div>
                  </td>
                  <td className="mono" style={{ fontSize: 12.5 }}>{e.resource}</td>
                  <td className="mono text-muted" style={{ fontSize: 12 }}>{e.ip || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <Sheet open={!!diff} onClose={() => setDiff(null)} eyebrow="Audit entry" title={diff?.action || ""} wide>
        {diff && <div><div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}><Badge mono>{diff.id}</Badge><Badge kind="muted">{diff.category}</Badge><span className="t-sm text-secondary">{diff.actor} &middot; <Time absolute ago={diff.ts || diff.timestamp} /></span></div>
          <div className="t-caption text-muted" style={{ marginBottom: 6 }}>Resource</div><div className="code" style={{ marginBottom: 16 }}>{diff.resource}</div>
          {diff.hash && <><div className="t-caption text-muted" style={{ marginTop: 16, marginBottom: 6 }}>Verified hash</div><div className="code" style={{ wordBreak: "break-all", fontSize: 11, whiteSpace: "pre-wrap" }}>{diff.hash}</div></>}
        </div>}
      </Sheet>
    </div>
  );
}

"use client";
import { useState, useRef, useEffect } from "react";
import { createPortal } from "react-dom";
import { Icons } from "@/components/icons";
import { Badge, Segmented, Sheet, SkeletonTable, Time } from "@/components/ui";
import { useApi, api } from "@/lib/api-client";

export default function AuditPage() {
  const [q, setQ] = useState("");
  const [time, setTime] = useState("7d");
  const [popover, setPopover] = useState<string | null>(null);
  const [diff, setDiff] = useState<any>(null);
  // Per-event integrity verdicts, fetched lazily when a badge is opened.
  // null = not checked, "loading" = in flight, else the verify result.
  const [verifyState, setVerifyState] = useState<Record<string, "loading" | { valid: boolean; sequence_no: number | null; error: string | null }>>({});
  // Popover is portaled to document.body (fixed position) so it isn't clipped
  // by the table's overflow container; coords come from the badge's rect.
  const [popCoords, setPopCoords] = useState<{ top: number; left: number; caret: number } | null>(null);
  const popRef = useRef<HTMLDivElement>(null);

  async function openVerify(id: string, el: HTMLElement) {
    const r = el.getBoundingClientRect();
    const POP_W = 250;
    // Open down-and-right from the badge, with the caret near the popover's left
    // edge pointing up at the badge. Anchoring to the badge's left (rather than
    // centring on it) means the popover never extends left of the badge — so it
    // can never slide under the sidebar, which always sits left of the table.
    // The badge center is ~16px in from the popover's left; the caret tracks it.
    const caretInset = 16;
    let left = r.left + r.width / 2 - caretInset;
    // Defensive bounds: never past the right viewport edge, and never left of the
    // sidebar (read its real width so collapsed/expanded both work).
    const styles = getComputedStyle(document.documentElement);
    const app = document.querySelector(".app") as HTMLElement | null;
    const collapsed = app?.getAttribute("data-collapsed") === "true";
    const sidebarW = parseInt(styles.getPropertyValue(collapsed ? "--sidebar-w-collapsed" : "--sidebar-w")) || 56;
    left = Math.max(sidebarW + 8, Math.min(left, window.innerWidth - POP_W - 12));
    setPopCoords({ top: r.bottom + 10, left, caret: r.left + r.width / 2 - left });
    setPopover(id);
    if (verifyState[id] && verifyState[id] !== "loading") return; // already have it
    setVerifyState((s) => ({ ...s, [id]: "loading" }));
    try {
      const res = await api.get(`/api/audit/events/${id}/verify`);
      const v = (res as { data?: { valid?: boolean; sequence_no?: number | null; error?: string | null } })?.data;
      setVerifyState((s) => ({ ...s, [id]: { valid: v?.valid === true, sequence_no: v?.sequence_no ?? null, error: v?.error ?? null } }));
    } catch {
      setVerifyState((s) => ({ ...s, [id]: { valid: false, sequence_no: null, error: "unreachable" } }));
    }
  }

  useEffect(() => { const onDoc = (e: MouseEvent) => { const t = e.target as HTMLElement; if (t.closest?.("[data-verify-badge]")) return; if (popRef.current && !popRef.current.contains(t)) setPopover(null); }; document.addEventListener("mousedown", onDoc); return () => document.removeEventListener("mousedown", onDoc); }, []);

  const params: Record<string, string> = { range: time };
  if (q) params.search = q;
  const { data, loading, error, refetch } = useApi<{ data: any[] }>("/api/audit", params, [q, time]);
  const { data: anchorData } = useApi<{ data: { anchored: boolean; anchored_at?: string | null; event_count?: number | null; signed?: boolean } }>("/api/audit/anchors");
  const anchor = anchorData?.data;
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
          <div className="page-subtitle">Tamper-evident. Every entry is hash-chained and periodically anchored.
            {anchor?.anchored ? (
              <span style={{ marginLeft: 8, display: "inline-flex", alignItems: "center", gap: 4, color: "var(--success)" }} title="A Merkle root over the audit chain has been sealed; altering any entry would change the root.">
                <Icons.ShieldCheck size={13} /> Chain anchored{anchor.anchored_at ? <> &middot; <Time ago={anchor.anchored_at} /></> : null}
              </span>
            ) : anchor && !anchor.anchored ? (
              <span style={{ marginLeft: 8, display: "inline-flex", alignItems: "center", gap: 4, color: "var(--text-muted)" }} title="No integrity anchor has been sealed yet. An anchor is sealed once events exist.">
                <Icons.Shield size={13} /> Not yet anchored
              </span>
            ) : null}
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
                    {(() => {
                      const v = verifyState[e.id];
                      const checked = v && v !== "loading";
                      const valid = checked && (v as { valid: boolean }).valid;
                      // The badge keeps one shape and lets color carry the
                      // state: a neutral shield before a check, the same shield
                      // in green once the HMAC verifies, and a red alert only if
                      // it does not. This matches the popover head exactly, so
                      // "verified" reads the same everywhere.
                      const bg = !checked ? "var(--bg-input)" : valid ? "var(--success-bg)" : "var(--danger-bg)";
                      const fg = !checked ? "var(--text-muted)" : valid ? "var(--success)" : "var(--danger)";
                      return (
                        <button title={checked ? (valid ? "Entry verified" : "Verification failed") : "Inspect this entry"} data-verify-badge onClick={(ev) => { ev.stopPropagation(); openVerify(e.id, ev.currentTarget); }}
                          style={{ width: 22, height: 22, borderRadius: 5, display: "grid", placeItems: "center", background: bg, color: fg }}>
                          {checked && !valid ? <Icons.AlertTriangle size={11} /> : <Icons.ShieldCheck size={11} />}
                        </button>
                      );
                    })()}
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
      {popover && popCoords && typeof document !== "undefined" && createPortal(
        (() => {
          const v = verifyState[popover];
          return (
            <div ref={popRef} className="verify-pop" style={{ top: popCoords.top, left: popCoords.left }}>
              <span className="verify-pop-caret" style={{ left: Math.max(14, Math.min(popCoords.caret, 250 - 14)) }} />
              {!v || v === "loading" ? (
                <div className="verify-pop-loading"><span className="spinner" /><span>Verifying integrity…</span></div>
              ) : (() => {
                const r = v as { valid: boolean; sequence_no: number | null; error: string | null };
                return (
                  <>
                    <div className={`verify-pop-head ${r.valid ? "ok" : "bad"}`}>
                      {r.valid ? <Icons.ShieldCheck size={15} /> : <Icons.AlertTriangle size={15} />}
                      <span>{r.valid ? "Integrity verified" : "Verification failed"}</span>
                    </div>
                    <p className="verify-pop-body">
                      {r.valid
                        ? "This entry\u2019s HMAC was recomputed and matches the hash chain. It has not been altered."
                        : `This entry did not pass the hash-chain check${r.error ? ` (${r.error})` : ""}.`}
                    </p>
                    {r.sequence_no != null && (
                      <div className="verify-pop-meta">
                        <span>Chain position</span>
                        <span className="mono">#{r.sequence_no.toLocaleString()}</span>
                      </div>
                    )}
                  </>
                );
              })()}
            </div>
          );
        })(),
        document.body,
      )}
      <Sheet open={!!diff} onClose={() => setDiff(null)} eyebrow="Audit entry" title={diff?.action || ""} wide>
        {diff && <div><div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}><Badge mono>{diff.id}</Badge><Badge kind="muted">{diff.category}</Badge><span className="t-sm text-secondary">{diff.actor} &middot; <Time absolute ago={diff.ts || diff.timestamp} /></span></div>
          <div className="t-caption text-muted" style={{ marginBottom: 6 }}>Resource</div><div className="code" style={{ marginBottom: 16 }}>{diff.resource}</div>
        </div>}
      </Sheet>
    </div>
  );
}

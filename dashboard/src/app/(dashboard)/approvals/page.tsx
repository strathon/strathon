"use client";
import { useState } from "react";
import { Icons } from "@/components/icons";
import { Badge, Segmented, Empty, Ring, Modal, Skeleton, useToast } from "@/components/ui";
import { useApi, api } from "@/lib/api-client";

export default function ApprovalsPage() {
  const toast = useToast();
  const [tab, setTab] = useState("pending");
  const [denyModal, setDenyModal] = useState<{ id: string; agent: string } | null>(null);
  const { data, loading, error, refetch } = useApi<{ data: any[] }>("/api/approvals", tab === "pending" ? { status: "pending" } : { status: "resolved" }, [tab]);
  const items = data?.data || [];

  async function handleAction(id: string, action: "approve" | "deny") {
    try {
      await api.post(`/api/approvals/${id}?action=${action}`);
      toast.push({ tone: action === "approve" ? "success" : "warning", title: action === "approve" ? "Approved" : "Denied" });
      refetch();
    } catch (e) { toast.push({ tone: "danger", title: "Action failed", body: e instanceof Error ? e.message : "Unknown error" }); }
  }

  if (error) return <div className="page"><div className="card" style={{ padding: 24, textAlign: "center" }}><div style={{ color: "var(--danger)", marginBottom: 8 }}>{error}</div><button className="btn" onClick={refetch}>Retry</button></div></div>;

  return (
    <div className="page">
      <div className="page-header"><div><h1 className="t-h1 page-title">Approvals</h1><div className="page-subtitle">{items.length} {tab}</div></div>
        <Segmented value={tab} onChange={setTab} options={[{ label: "Pending", value: "pending" }, { label: "History", value: "resolved" }]} />
      </div>
      {loading ? <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))", gap: 12 }}>{[1,2,3].map(i => <Skeleton key={i} width="100%" height={160} style={{ borderRadius: 12 }} />)}</div> : items.length === 0 ? (
        <Empty icon={<Icons.UserCheck size={24} />} title={tab === "pending" ? "All clear" : "No history yet"} subtitle={tab === "pending" ? "No approvals waiting. Your agents are operating within policy." : "Resolved approvals will appear here."} />
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))", gap: 12 }}>
          {items.map((a: any) => (
            <div key={a.id} className="card">
              <div style={{ display: "flex", alignItems: "flex-start", gap: 12, marginBottom: 12 }}>
                <div style={{ width: 40, height: 40, borderRadius: 8, background: "var(--warning-bg)", color: "var(--warning)", display: "grid", placeItems: "center", flexShrink: 0 }}><Icons.UserCheck size={18} /></div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontWeight: 600 }}>{a.agent}</div>
                  <div className="t-sm text-secondary mono">{a.tool}</div>
                  <div className="t-sm text-muted">{a.policy}</div>
                </div>
                {a.expiresIn > 0 && <Ring value={Math.min(100, (a.expiresIn / 420) * 100)} size={40} stroke={3} color="var(--warning)" label={`${Math.floor(a.expiresIn / 60)}m`} />}
              </div>
              {a.params && <div className="code" style={{ fontSize: 11, marginBottom: 12, maxHeight: 80, overflow: "auto" }}>{JSON.stringify(a.params, null, 2)}</div>}
              {tab === "pending" && (
                <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                  <button className="btn ghost sm" onClick={() => setDenyModal({ id: a.id, agent: a.agent })}>Deny</button>
                  <button className="btn primary sm" onClick={() => handleAction(a.id, "approve")}><Icons.Check size={12} /> Approve</button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      <Modal open={!!denyModal} onClose={() => setDenyModal(null)} title="Deny this request?" danger confirmLabel="Deny"
        body={<>Deny the approval from <strong>{denyModal?.agent}</strong>? The tool call will be blocked.</>}
        onConfirm={() => { if (denyModal) handleAction(denyModal.id, "deny"); setDenyModal(null); }} />
    </div>
  );
}

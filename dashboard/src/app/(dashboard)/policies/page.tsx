"use client";

import { useState, useMemo } from "react";
import { useRouter } from "next/navigation";
import { Icons } from "@/components/icons";
import { Badge, StatusBadge, Sparkline, Checkbox, Dropdown, Pagination, Modal, Sheet, Segmented, Empty, Time, Kbd, SkeletonTable, useToast } from "@/components/ui";
import { useApi, api } from "@/lib/api-client";
import { usePermissions } from "@/lib/permissions";

const ACTION_COLOR: Record<string, string> = { block: "danger", steer: "warning", throttle: "warning", log: "muted", alert: "info", require_approval: "info" };

export default function PoliciesPage() {
  const router = useRouter();
  const toast = useToast();
  const perms = usePermissions();
  const [q, setQ] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirm, setConfirm] = useState<{ id: string; name: string } | null>(null);
  const [bulkConfirm, setBulkConfirm] = useState<string | null>(null);
  const [chartSheet, setChartSheet] = useState<any>(null);
  const pageSize = 8;

  const params: Record<string, string> = { limit: String(pageSize), offset: String((page - 1) * pageSize) };
  if (q) params.search = q;
  if (statusFilter !== "all") params.status = statusFilter;

  const { data, loading, error, refetch } = useApi<{ data: any[]; total?: number }>("/api/policies", params, [q, statusFilter, page]);

  const policies = data?.data || [];
  const total = data?.total || policies.length;

  if (error) return (
    <div className="page">
      <div className="card" style={{ padding: 24, textAlign: "center" }}>
        <div style={{ color: "var(--danger)", marginBottom: 8 }}>{error}</div>
        <button className="btn" onClick={refetch}>Retry</button>
      </div>
    </div>
  );

  async function handleDelete(id: string, name: string) {
    try {
      await api.del(`/api/policies/${id}`);
      toast.push({ tone: "success", title: "Policy deleted", body: name });
      refetch();
    } catch (e) {
      toast.push({ tone: "danger", title: "Failed to delete", body: e instanceof Error ? e.message : "Unknown error" });
    }
  }

  async function handleBulkDelete() {
    const count = selected.size;
    try {
      await Promise.all([...selected].map((id) => api.del(`/api/policies/${id}`)));
      toast.push({ tone: "success", title: `${count} ${count === 1 ? "policy" : "policies"} deleted` });
      setSelected(new Set());
      refetch();
    } catch (e) {
      toast.push({ tone: "danger", title: "Bulk delete failed", body: e instanceof Error ? e.message : "Unknown error" });
    }
  }

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1 className="t-h1 page-title">Policies</h1>
          <div className="page-subtitle">{total} {total === 1 ? "policy" : "policies"}</div>
        </div>
        {perms.canWritePolicies && <button className="btn primary" onClick={() => router.push("/policies/new")}><Icons.Plus size={13} /> New policy</button>}
      </div>

      <div className="table-toolbar">
        <div className="input-wrap" style={{ width: 320 }}>
          <Icons.Search size={14} />
          <input className="input search" placeholder="Search by name\u2026" value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} />
        </div>
        <Dropdown width={180} trigger={({ toggle }) => (
          <button className="btn" onClick={toggle}><Icons.Filter size={13} /> Status: {statusFilter === "all" ? "All" : statusFilter} <Icons.ChevronDown size={13} /></button>
        )} items={[
          { label: "All", onClick: () => { setStatusFilter("all"); setPage(1); } },
          { label: "Enabled", onClick: () => { setStatusFilter("enabled"); setPage(1); } },
          { label: "Shadow", onClick: () => { setStatusFilter("shadow"); setPage(1); } },
          { label: "Disabled", onClick: () => { setStatusFilter("disabled"); setPage(1); } },
        ]} />
        <div className="grow" />
        <span className="text-muted t-sm">{total} policies</span>
      </div>

      {selected.size > 0 && (
        <div className="bulk-bar">
          <span className="bulk-bar-count">{selected.size} selected</span>
          <div style={{ flex: 1 }} />
          <button className="btn danger sm" onClick={() => setBulkConfirm("delete")}><Icons.Trash size={12} /> Delete</button>
          <button className="btn ghost sm" onClick={() => setSelected(new Set())}>Clear</button>
        </div>
      )}

      <div className="table-wrap">
        {loading ? <SkeletonTable rows={6} columns={[3, 1, 1, 1, 1.5, 1]} /> : policies.length === 0 ? (
          <Empty icon={<Icons.Shield size={24} />} title="No policies yet" subtitle="Create your first policy to start enforcing rules on agent behavior."
            action={perms.canWritePolicies ? <button className="btn primary" onClick={() => router.push("/policies/new")}><Icons.Plus size={13} /> New policy</button> : undefined} />
        ) : (
          <>
            <table className="table">
              <thead><tr>
                <th style={{ width: 36, padding: "10px 0 10px 16px" }}>
                  <Checkbox checked={policies.length > 0 && policies.every((r: any) => selected.has(r.id))}
                    onChange={() => { const all = policies.every((r: any) => selected.has(r.id)); setSelected((s) => { const n = new Set(s); if (all) policies.forEach((r: any) => n.delete(r.id)); else policies.forEach((r: any) => n.add(r.id)); return n; }); }} />
                </th>
                <th style={{ width: "30%" }}>Name</th><th>Status</th><th>Action</th><th style={{ textAlign: "right" }}>Priority</th><th>Hits 7d</th><th>Modified</th><th style={{ width: 40 }} />
              </tr></thead>
              <tbody>
                {policies.map((p: any) => (
                  <tr key={p.id} className="clickable" onClick={() => router.push(`/policies/${p.id}`)}>
                    <td onClick={(e) => e.stopPropagation()} style={{ width: 36, padding: "0 0 0 16px" }}>
                      <Checkbox checked={selected.has(p.id)} onChange={() => setSelected((s) => { const n = new Set(s); if (n.has(p.id)) n.delete(p.id); else n.add(p.id); return n; })} />
                    </td>
                    <td><div style={{ fontWeight: 600 }}>{p.name}</div><div className="t-sm text-muted" style={{ marginTop: 2, maxWidth: 420, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p.description}</div></td>
                    <td>{StatusBadge[p.status as keyof typeof StatusBadge]?.()}</td>
                    <td><Badge kind={ACTION_COLOR[p.action] || "muted"} mono>{p.action}</Badge></td>
                    <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{p.priority}</td>
                    <td>{p.hits7d && <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <span onClick={(e) => { e.stopPropagation(); setChartSheet(p); }} style={{ cursor: "pointer" }}><Sparkline data={p.hits7d} width={64} height={20} color="var(--accent)" /></span>
                      <span className="t-sm text-muted" style={{ fontVariantNumeric: "tabular-nums" }}>{p.hits7d.reduce((a: number, b: number) => a + b, 0)}</span>
                    </div>}</td>
                    <td className="text-secondary t-sm"><Time ago={p.lastModified || p.last_modified} /></td>
                    <td>
                      <div className="row-menu" onClick={(e) => e.stopPropagation()}>
                        <Dropdown align="right" width={180} trigger={({ toggle }) => <button className="btn icon ghost sm" onClick={toggle}><Icons.MoreHorizontal size={14} /></button>}
                          items={[
                            { icon: <Icons.Edit size={13} />, label: "Edit", onClick: () => router.push(`/policies/${p.id}`) },
                            { divider: true },
                            { icon: <Icons.Trash size={13} />, label: "Delete", danger: true, onClick: () => setConfirm({ id: p.id, name: p.name }) },
                          ]} />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Pagination total={total} page={page} pageSize={pageSize} onPage={setPage} />
          </>
        )}
      </div>

      <Modal open={!!confirm} onClose={() => setConfirm(null)} title="Delete this policy?" danger confirmLabel="Delete policy"
        body={<>You&apos;re about to delete <code className="mono">{confirm?.name}</code>. This cannot be undone.</>}
        onConfirm={() => { if (confirm) handleDelete(confirm.id, confirm.name); }} />

      <Modal open={bulkConfirm === "delete"} onClose={() => setBulkConfirm(null)} title={`Delete ${selected.size} policies?`} danger confirmLabel="Delete selected"
        body="Every selected policy will be removed. This cannot be undone."
        onConfirm={handleBulkDelete} />

      <Sheet open={!!chartSheet} onClose={() => setChartSheet(null)} eyebrow="Hits over time" title={chartSheet?.name || ""} wide>
        {chartSheet?.hits7d && (
          <div className="card" style={{ padding: 16 }}>
            <Sparkline data={chartSheet.hits7d} width={600} height={180} color="var(--accent)" />
          </div>
        )}
      </Sheet>
    </div>
  );
}

"use client";

import { useState, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Icons } from "@/components/icons";
import { Badge, useToast, Skeleton, Modal, Dropdown, Empty } from "@/components/ui";
import { GeneralSettings, RetentionSliders, ExportSection, ApiKeysSection } from "./_parts";
import { MfaCard } from "./MfaCard";
import { useUser } from "@/lib/user-context";
import { useApi, api } from "@/lib/api-client";
import { validatePassword } from "@/lib/validation";
import { formatDate, formatRelative } from "@/lib/format";

export default function SettingsPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const toast = useToast();
  const { user: currentUser, mode, refetch } = useUser();
  const [section, setSection] = useState(searchParams.get("section") || "general");
  const [inviteOpen, setInviteOpen] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("operator");
  const [removeConfirm, setRemoveConfirm] = useState<{ id: string; name: string } | null>(null);
  const [tempPassword, setTempPassword] = useState<{ name: string; password: string } | null>(null);
  const [transferTarget, setTransferTarget] = useState<{ id: string; name: string } | null>(null);
  const [disableMfaTarget, setDisableMfaTarget] = useState<{ id: string; name: string } | null>(null);
  const [deleteAccountText, setDeleteAccountText] = useState("");
  const [deleteAccountOpen, setDeleteAccountOpen] = useState(false);

  // Change password form state
  const [currentPass, setCurrentPass] = useState("");
  const [newPass, setNewPass] = useState("");
  const [confirmPass, setConfirmPass] = useState("");
  const [changingPass, setChangingPass] = useState(false);
  const [passError, setPassError] = useState<string | null>(null);

  useEffect(() => {
    const s = searchParams.get("section");
    if (s && s !== section) setSection(s);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  const { data: membersData, loading: membersLoading, refetch: refetchMembers } = useApi<{ data: any[] }>("/api/members");
  const members = membersData?.data || [];
  const { data: pendingData, refetch: refetchPending } = useApi<{ data: any[] }>("/api/members/pending");
  const pending = pendingData?.data || [];

  const isAdmin = currentUser?.role === "owner" || currentUser?.role === "admin";

  const allSections = [
    { id: "general", label: "General" },
    { id: "members", label: "Members" },
    { id: "authentication", label: "Authentication" },
    { id: "apikeys", label: "API keys" },
    { id: "retention", label: "Retention" },
    { id: "export", label: "Export" },
    ...(mode === "cloud" ? [{ id: "billing", label: "Billing" }] : []),
    { id: "account", label: "Account" },
  ];

  const changePassword = async () => {
    if (!currentPass) { setPassError("Current password is required"); return; }
    const passErr = validatePassword(newPass);
    if (passErr) { setPassError(passErr); return; }
    if (newPass !== confirmPass) { setPassError("Passwords do not match"); return; }
    setChangingPass(true);
    setPassError(null);
    try {
      await api.post("/api/auth/change-password", { current_password: currentPass, new_password: newPass });
      toast.push({ tone: "success", title: "Password changed" });
      setCurrentPass(""); setNewPass(""); setConfirmPass("");
      refetch();
    } catch (err) {
      setPassError(err instanceof Error ? err.message : "Failed");
    } finally {
      setChangingPass(false);
    }
  };

  const resetMemberPassword = async (memberId: string, memberName: string) => {
    try {
      const res = await api.post(`/api/members/${memberId}/reset-password`);
      setTempPassword({ name: memberName, password: res.temporary_password || res.password });
    } catch (err) {
      toast.push({ tone: "danger", title: err instanceof Error ? err.message : "Failed" });
    }
  };

  const disableMemberMfa = async (memberId: string) => {
    try {
      await api.post(`/api/members/${memberId}/disable-mfa`);
      toast.push({ tone: "success", title: "MFA disabled for member" });
      setDisableMfaTarget(null);
      refetchMembers();
    } catch (err) {
      toast.push({ tone: "danger", title: err instanceof Error ? err.message : "Failed" });
    }
  };

  const transferOwnership = async (memberId: string) => {
    try {
      await api.post(`/api/members/${memberId}/transfer-ownership`);
      toast.push({ tone: "success", title: "Ownership transferred" });
      setTransferTarget(null);
      refetchMembers();
      refetch();
    } catch (err) {
      toast.push({ tone: "danger", title: err instanceof Error ? err.message : "Failed" });
    }
  };

  const deleteAccount = async () => {
    try {
      await api.del("/api/auth/me");
      window.location.href = "/login";
    } catch (err) {
      toast.push({ tone: "danger", title: err instanceof Error ? err.message : "Failed" });
    }
  };

  const exportMyData = async () => {
    try {
      const res = await fetch("/api/auth/me/export", { credentials: "same-origin" });
      if (res.ok) {
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a"); a.href = url; a.download = "strathon-my-data.json"; a.click();
        URL.revokeObjectURL(url);
        toast.push({ tone: "success", title: "Data exported" });
      }
    } catch {
      toast.push({ tone: "danger", title: "Export failed" });
    }
  };

  return (
    <div className="page narrow settings-layout">
      <nav className="settings-sidebar">
        <h1 className="settings-sidebar-title">Settings</h1>
        {allSections.map((s) => (
          <button key={s.id} className="settings-nav-item" data-active={section === s.id}
            onClick={() => { setSection(s.id); router.push(`/settings?section=${s.id}`); }}>
            {s.label}
          </button>
        ))}
      </nav>

      <div>
        {section === "general" && <GeneralSettings />}

        {section === "members" && (
          <>
            <h2 className="t-h2" style={{ marginBottom: 4 }}>Members</h2>
            <p className="text-secondary" style={{ marginBottom: 24 }}>People with access to this workspace.</p>
            <div className="table-toolbar">
              <div className="input-wrap" style={{ flex: 1, maxWidth: 320 }}><Icons.Search size={14} /><input className="input search" placeholder="Search members&hellip;" /></div>
              {isAdmin && <button className="btn primary" onClick={() => setInviteOpen(true)}><Icons.Plus size={13} /> Invite member</button>}
            </div>
            <div className="table-wrap">
              {membersLoading ? <Skeleton width="100%" height={200} /> : (members.length === 0 && pending.length === 0) ? (
                <Empty icon={<Icons.Users size={24} />} title="No team members yet"
                  subtitle="Invite people to collaborate on this workspace." />
              ) : (
              <table className="table">
                <thead><tr><th>Member</th><th>Role</th><th>Joined</th><th>Last active</th><th /></tr></thead>
                <tbody>
                  {members.map((m: any) => {
                    const isSelf = m.email === currentUser?.email;
                    const isOwner = m.role === "owner";
                    return (
                    <tr key={m.id || m.email}>
                      <td>
                        <div style={{ display: "flex", alignItems: "center", gap: 10, height: 48 }}>
                          <div className="avatar" style={{ width: 28, height: 28, background: "var(--accent-bg)", color: "var(--accent)", border: "1px solid var(--accent-border)" }}>{(m.display_name || m.name || m.email).split(" ").map((s: string) => s[0]).join("").slice(0, 2)}</div>
                          <div><div style={{ fontWeight: 500 }}>{m.display_name || m.name}{isSelf && <span className="text-muted"> (you)</span>}</div><div className="t-sm text-muted">{m.email}</div></div>
                        </div>
                      </td>
                      <td>
                        {isAdmin && !isOwner && !isSelf ? (
                        <select className="select" value={m.role} style={{ width: 130 }}
                          onChange={async (e) => {
                            try { await api.patch(`/api/members/${m.id}`, { role: e.target.value }); toast.push({ tone: "success", title: "Role updated" }); refetchMembers(); }
                            catch (err) { toast.push({ tone: "danger", title: err instanceof Error ? err.message : "Failed" }); }
                          }}>
                          <option value="admin">Admin</option><option value="operator">Operator</option><option value="viewer">Viewer</option>
                        </select>
                        ) : (
                          <Badge kind={isOwner ? "accent" : "muted"}>{m.role}</Badge>
                        )}
                      </td>
                      <td className="text-secondary">{m.joined_at || m.joined ? formatDate(m.joined_at || m.joined) : ""}</td>
                      <td className="text-secondary">{m.last_active ? formatRelative(m.last_active) : ""}</td>
                      <td>
                        {isAdmin && !isSelf && (
                          <Dropdown align="right" width={200}
                            trigger={({ toggle }) => <button className="btn icon ghost sm" onClick={toggle}><Icons.MoreHorizontal size={14} /></button>}
                            items={[
                              ...(!isOwner ? [{ icon: <Icons.Key size={13} />, label: "Reset password", onClick: () => resetMemberPassword(m.id, m.display_name || m.name || m.email) }] : []),
                              ...(!isOwner ? [{ icon: <Icons.Lock size={13} />, label: "Disable MFA", onClick: () => setDisableMfaTarget({ id: m.id, name: m.display_name || m.name || m.email }) }] : []),
                              ...(currentUser?.role === "owner" && m.role === "admin" ? [{ icon: <Icons.Shield size={13} />, label: "Transfer ownership", onClick: () => setTransferTarget({ id: m.id, name: m.display_name || m.name || m.email }) }] : []),
                              ...(!isOwner ? [{ divider: true }, { icon: <Icons.Trash size={13} />, label: "Remove", danger: true, onClick: () => setRemoveConfirm({ id: m.id, name: m.display_name || m.name || m.email }) }] : []),
                            ]} />
                        )}
                      </td>
                    </tr>
                    );
                  })}
                  {pending.map((p: any) => (
                    <tr key={`pending-${p.email}`} style={{ opacity: 0.75 }}>
                      <td>
                        <div style={{ display: "flex", alignItems: "center", gap: 10, height: 48 }}>
                          <div className="avatar" style={{ width: 28, height: 28, background: "var(--text-muted)", color: "#fff" }}>{p.email.slice(0, 2).toUpperCase()}</div>
                          <div><div style={{ fontWeight: 500 }}>{p.email}</div><div className="t-sm text-muted">Invitation not yet accepted</div></div>
                        </div>
                      </td>
                      <td><Badge kind="muted">{p.role}</Badge></td>
                      <td><Badge kind="warning">pending</Badge></td>
                      <td className="text-secondary">{p.invited_at ? formatDate(p.invited_at) : ""}</td>
                      <td>
                        {isAdmin && (
                          <button className="btn icon ghost sm" title="Revoke invitation"
                            onClick={async () => {
                              try { await api.del(`/api/members/pending/${encodeURIComponent(p.email)}`); toast.push({ tone: "success", title: "Invitation revoked" }); refetchPending(); }
                              catch (err) { toast.push({ tone: "danger", title: err instanceof Error ? err.message : "Failed" }); }
                            }}><Icons.Trash size={14} /></button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              )}
            </div>
            {inviteOpen && (
              <div className="card" style={{ marginTop: 16 }}>
                <div className="card-header"><span className="card-title">Invite member</span></div>
                <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
                  <div style={{ flex: 1 }}><div className="form-label">Email</div><input className="input" value={inviteEmail} onChange={(e) => setInviteEmail(e.target.value)} placeholder="name@company.com" /></div>
                  <div><div className="form-label">Role</div><select className="select" value={inviteRole} onChange={(e) => setInviteRole(e.target.value)} style={{ width: 130 }}><option value="admin">Admin</option><option value="operator">Operator</option><option value="viewer">Viewer</option></select></div>
                  <button className="btn primary" onClick={async () => {
                    try { await api.post("/api/members", { email: inviteEmail, role: inviteRole }); toast.push({ tone: "success", title: "Invitation sent" }); setInviteOpen(false); setInviteEmail(""); refetchMembers(); refetchPending(); }
                    catch (err) { toast.push({ tone: "danger", title: err instanceof Error ? err.message : "Failed" }); }
                  }}>Invite</button>
                  <button className="btn ghost" onClick={() => setInviteOpen(false)}>Cancel</button>
                </div>
              </div>
            )}
          </>
        )}

        {section === "authentication" && (
          <>
            <h2 className="t-h2" style={{ marginBottom: 4 }}>Authentication</h2>
            <p className="text-secondary" style={{ marginBottom: 24 }}>Manage your sign-in methods.</p>
            <MfaCard />
            <div className="card">
              <div className="card-header"><span className="card-title">Change password</span></div>
              <div className="col" style={{ gap: 12, maxWidth: 360 }}>
                <input className="input" type="password" placeholder="Current password" value={currentPass} onChange={(e) => setCurrentPass(e.target.value)} autoComplete="current-password" />
                <input className="input" type="password" placeholder="New password" value={newPass} onChange={(e) => setNewPass(e.target.value)} autoComplete="new-password" />
                <input className="input" type="password" placeholder="Confirm new password" value={confirmPass} onChange={(e) => setConfirmPass(e.target.value)} autoComplete="new-password" />
                {passError && <div className="t-sm" style={{ color: "var(--danger)" }}>{passError}</div>}
                <div style={{ display: "flex", justifyContent: "flex-end" }}>
                  <button className="btn primary" onClick={changePassword} disabled={changingPass}>
                    {changingPass ? "Updating\u2026" : "Update password"}
                  </button>
                </div>
              </div>
            </div>
          </>
        )}

        {section === "retention" && (
          <>
            <h2 className="t-h2" style={{ marginBottom: 4 }}>Data retention</h2>
            <p className="text-secondary" style={{ marginBottom: 24 }}>How long Strathon stores each data type before automatic deletion.</p>
            <RetentionSliders />
          </>
        )}

        {section === "export" && <ExportSection />}

        {section === "apikeys" && <ApiKeysSection />}

        {section === "billing" && (
          <>
            <h2 className="t-h2" style={{ marginBottom: 4 }}>Billing</h2>
            <p className="text-secondary" style={{ marginBottom: 24 }}>Plan, payment, invoices.</p>
            <div className="card" style={{ marginBottom: 16 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                <div>
                  <div className="t-caption text-muted">Current plan</div>
                  <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginTop: 4 }}><span className="t-h2">Team</span><span className="text-secondary">$199 / month</span></div>
                  <div className="t-sm text-secondary" style={{ marginTop: 4 }}>5 seats &middot; 250k traces / mo &middot; 90-day retention</div>
                </div>
                <button className="btn primary">Upgrade to Enterprise</button>
              </div>
            </div>
          </>
        )}

        {section === "account" && (
          <>
            <h2 className="t-h2" style={{ marginBottom: 4 }}>Account</h2>
            <p className="text-secondary" style={{ marginBottom: 24 }}>Data portability and account management.</p>
            <div className="card" style={{ marginBottom: 16 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <div>
                  <div style={{ fontWeight: 600 }}>Export my data</div>
                  <div className="t-sm text-secondary" style={{ marginTop: 2 }}>Download a JSON file containing all your personal data (GDPR Article 20).</div>
                </div>
                <button className="btn" onClick={exportMyData}><Icons.Download size={13} /> Export</button>
              </div>
            </div>
            <div style={{ border: "1px solid color-mix(in oklab, var(--danger) 38%, transparent)", borderRadius: 8, overflow: "hidden", background: "var(--bg-deep)" }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "16px 18px", gap: 16 }}>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontWeight: 600, fontSize: 13.5, marginBottom: 2 }}>Delete my account</div>
                  <div className="t-sm text-secondary">Permanently remove your account and anonymize your audit entries. This cannot be undone.</div>
                </div>
                <button className="btn danger" style={{ flexShrink: 0 }} onClick={() => setDeleteAccountOpen(true)}>Delete account</button>
              </div>
            </div>
          </>
        )}
      </div>

      {/* Modals */}
      <Modal open={!!removeConfirm} onClose={() => setRemoveConfirm(null)} title="Remove member?" danger confirmLabel="Remove"
        body={<>Remove <strong>{removeConfirm?.name}</strong> from this workspace? They will lose all access.</>}
        onConfirm={async () => { if (!removeConfirm) return; try { await api.del(`/api/members/${removeConfirm.id}`); toast.push({ tone: "success", title: "Member removed" }); refetchMembers(); } catch (err) { toast.push({ tone: "danger", title: err instanceof Error ? err.message : "Failed" }); } }} />

      <Modal open={!!tempPassword} onClose={() => setTempPassword(null)} title="Temporary password"
        body={tempPassword ? (
          <div>
            <p className="text-secondary" style={{ marginBottom: 12 }}>Give this temporary password to <strong>{tempPassword.name}</strong>. They will be required to change it on next login.</p>
            <div style={{ position: "relative" }}>
              <div className="code" style={{ wordBreak: "break-all", padding: "12px 50px 12px 14px" }}>{tempPassword.password}</div>
              <button className="btn icon ghost" style={{ position: "absolute", top: 6, right: 8 }} onClick={() => { navigator.clipboard?.writeText(tempPassword.password); toast.push({ tone: "success", title: "Copied" }); }}><Icons.Copy size={14} /></button>
            </div>
          </div>
        ) : null}
        confirmLabel="Done" onConfirm={() => setTempPassword(null)} />

      <Modal open={!!transferTarget} onClose={() => setTransferTarget(null)} title="Transfer ownership?" danger confirmLabel="Transfer"
        body={<>Transfer workspace ownership to <strong>{transferTarget?.name}</strong>? You will become an admin. This cannot be undone.</>}
        onConfirm={() => transferTarget && transferOwnership(transferTarget.id)} />

      <Modal open={!!disableMfaTarget} onClose={() => setDisableMfaTarget(null)} title="Disable MFA?" danger confirmLabel="Disable MFA"
        body={<>Disable multi-factor authentication for <strong>{disableMfaTarget?.name}</strong>? Only do this if they&apos;ve lost access to their authenticator and backup codes.</>}
        onConfirm={() => disableMfaTarget && disableMemberMfa(disableMfaTarget.id)} />

      <Modal open={deleteAccountOpen} onClose={() => { setDeleteAccountOpen(false); setDeleteAccountText(""); }} title="Delete your account?" danger
        confirmLabel="Delete my account"
        body={
          <div>
            <p className="text-secondary" style={{ marginBottom: 12 }}>This permanently deletes your account and anonymizes your audit log entries. This cannot be undone.</p>
            <div className="form-label">Type <b>{currentUser?.email}</b> to confirm</div>
            <input className="input" value={deleteAccountText} onChange={(e) => setDeleteAccountText(e.target.value)} placeholder={currentUser?.email || ""} />
          </div>
        }
        onConfirm={() => { if (deleteAccountText === (currentUser?.email || "")) deleteAccount(); }} />
    </div>
  );
}

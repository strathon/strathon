"use client";

import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { StrathonLogo } from "@/components/logo";
import { api } from "@/lib/api-client";
import { validatePassword } from "@/lib/validation";

export default function ChangePasswordPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const forced = searchParams.get("forced") === "true";

  const [current, setCurrent] = useState("");
  const [newPass, setNewPass] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!current) { setError("Current password is required"); return; }
    const passErr = validatePassword(newPass);
    if (passErr) { setError(passErr); return; }
    if (newPass !== confirm) { setError("Passwords do not match"); return; }

    setLoading(true);
    setError(null);
    try {
      await api.post("/api/auth/change-password", {
        current_password: current,
        new_password: newPass,
      });
      router.push("/overview");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Password change failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-mark">
          <div className="brand-mark" style={{ width: 36, height: 36 }}><StrathonLogo size={36} /></div>
          <span style={{ fontSize: 22, fontWeight: 600, letterSpacing: "-0.02em" }}>Strathon</span>
        </div>
        <form onSubmit={handleSubmit}>
          <h1 className="t-h2" style={{ marginBottom: 6 }}>Change password</h1>
          <p className="t-sm text-secondary" style={{ marginBottom: 22 }}>
            {forced ? "Your admin has required you to set a new password before continuing." : "Update your account password."}
          </p>
          <div className="form-row">
            <label className="form-label">Current password</label>
            <input className="input" type="password" value={current} onChange={(e) => setCurrent(e.target.value)} autoFocus autoComplete="current-password" />
          </div>
          <div className="form-row">
            <label className="form-label">New password</label>
            <input className="input" type="password" value={newPass} onChange={(e) => setNewPass(e.target.value)} placeholder="10+ characters" autoComplete="new-password" />
          </div>
          <div className="form-row">
            <label className="form-label">Confirm new password</label>
            <input className="input" type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" />
          </div>
          {error && <div className="t-sm" style={{ color: "var(--danger)", marginBottom: 10 }}>{error}</div>}
          <button className="btn primary" style={{ width: "100%", height: 38 }} type="submit" disabled={loading}>
            {loading ? "Updating\u2026" : "Update password"}
          </button>
          {!forced && (
            <div className="t-sm text-muted" style={{ marginTop: 18, textAlign: "center" }}>
              <a href="/settings?section=authentication" style={{ color: "var(--accent)" }}>Back to settings</a>
            </div>
          )}
        </form>
      </div>
    </div>
  );
}

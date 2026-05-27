"use client";

import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { StrathonLogo } from "@/components/logo";
import { api } from "@/lib/api-client";
import { validatePassword } from "@/lib/validation";

export default function ResetPasswordPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const token = searchParams.get("token");

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);

  if (!token) {
    return (
      <div className="login-screen">
        <div className="login-card">
          <h1 className="t-h2" style={{ marginBottom: 6 }}>Invalid link</h1>
          <p className="t-sm text-secondary" style={{ marginBottom: 22 }}>This reset link is missing or expired.</p>
          <a href="/login" className="btn primary" style={{ width: "100%", display: "block", textAlign: "center" }}>Back to sign in</a>
        </div>
      </div>
    );
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const passErr = validatePassword(password);
    if (passErr) { setError(passErr); return; }
    if (password !== confirm) { setError("Passwords do not match"); return; }

    setLoading(true);
    setError(null);
    try {
      await api.post("/api/auth/password-reset", { token, new_password: password });
      setDone(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Reset failed");
    } finally {
      setLoading(false);
    }
  }

  if (done) {
    return (
      <div className="login-screen">
        <div className="login-card">
          <div className="login-mark">
            <div className="brand-mark" style={{ width: 36, height: 36 }}><StrathonLogo size={36} /></div>
            <span style={{ fontSize: 22, fontWeight: 600, letterSpacing: "-0.02em" }}>Strathon</span>
          </div>
          <h1 className="t-h2" style={{ marginBottom: 6 }}>Password reset</h1>
          <p className="t-sm text-secondary" style={{ marginBottom: 22 }}>Your password has been changed. Sign in with your new password.</p>
          <button className="btn primary" style={{ width: "100%" }} onClick={() => router.push("/login")}>Sign in</button>
        </div>
      </div>
    );
  }

  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-mark">
          <div className="brand-mark" style={{ width: 36, height: 36 }}><StrathonLogo size={36} /></div>
          <span style={{ fontSize: 22, fontWeight: 600, letterSpacing: "-0.02em" }}>Strathon</span>
        </div>
        <form onSubmit={handleSubmit}>
          <h1 className="t-h2" style={{ marginBottom: 6 }}>New password</h1>
          <p className="t-sm text-secondary" style={{ marginBottom: 22 }}>Choose a new password for your account.</p>
          <div className="form-row">
            <label className="form-label">New password</label>
            <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="10+ characters" autoFocus autoComplete="new-password" />
          </div>
          <div className="form-row">
            <label className="form-label">Confirm password</label>
            <input className="input" type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" />
          </div>
          {error && <div className="t-sm" style={{ color: "var(--danger)", marginBottom: 10 }}>{error}</div>}
          <button className="btn primary" style={{ width: "100%", height: 38 }} type="submit" disabled={loading}>
            {loading ? "Saving\u2026" : "Set new password"}
          </button>
        </form>
      </div>
    </div>
  );
}

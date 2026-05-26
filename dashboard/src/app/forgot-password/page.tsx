"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { StrathonLogo } from "@/components/logo";
import { api } from "@/lib/api-client";
import { validateEmail } from "@/lib/validation";

export default function ForgotPasswordPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const emailErr = validateEmail(email);
    if (emailErr) { setError(emailErr); return; }

    setLoading(true);
    setError(null);
    try {
      await api.post("/api/auth/password-reset-request", { email });
      setSent(true);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Request failed";
      if (msg.includes("not configured")) {
        setError("Email not configured. Contact your admin.");
      } else {
        setSent(true);
      }
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
        {sent ? (
          <div>
            <h1 className="t-h2" style={{ marginBottom: 6 }}>Check your email</h1>
            <p className="t-sm text-secondary" style={{ marginBottom: 22 }}>
              If an account exists for that email, we sent a password reset link. It expires in 1 hour.
            </p>
            <button className="btn primary" style={{ width: "100%" }} onClick={() => router.push("/login")}>
              Back to sign in
            </button>
          </div>
        ) : (
          <form onSubmit={handleSubmit}>
            <h1 className="t-h2" style={{ marginBottom: 6 }}>Reset password</h1>
            <p className="t-sm text-secondary" style={{ marginBottom: 22 }}>
              Enter your email and we&apos;ll send a reset link.
            </p>
            <div className="form-row">
              <label className="form-label">Email</label>
              <input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" autoFocus autoComplete="email" />
            </div>
            {error && <div className="t-sm" style={{ color: "var(--danger)", marginBottom: 10 }}>{error}</div>}
            <button className="btn primary" style={{ width: "100%", height: 38 }} type="submit" disabled={loading}>
              {loading ? "Sending\u2026" : "Send reset link"}
            </button>
            <div className="t-sm text-muted" style={{ marginTop: 18, textAlign: "center" }}>
              <a href="/login" style={{ color: "var(--accent)" }}>Back to sign in</a>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

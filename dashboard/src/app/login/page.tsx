"use client";

import { useState, useRef, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Icons } from "@/components/icons";
import { StrathonLogo } from "@/components/logo";
import { api } from "@/lib/api-client";
import { setTheme, getStoredTheme, resolveTheme } from "@/lib/theme";
import { validateEmail, validatePassword } from "@/lib/validation";

interface Capabilities {
  registration_enabled: boolean;
  smtp_enabled: boolean;
  mfa_available: boolean;
  mode: string;
}

export default function LoginPage() {
  const router = useRouter();
  const [themeMode, setThemeMode] = useState<"light" | "dark">("dark");
  useEffect(() => { setThemeMode(resolveTheme(getStoredTheme())); }, []);
  function toggleTheme() {
    const next = themeMode === "dark" ? "light" : "dark";
    setTheme(next);
    setThemeMode(next);
  }
  const searchParams = useSearchParams();
  const expired = searchParams.get("expired") === "true";
  const resetDone = searchParams.get("reset") === "true";
  const redirect = searchParams.get("redirect") || "/overview";

  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [step, setStep] = useState<"creds" | "mfa" | "backup">("creds");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mfaToken, setMfaToken] = useState<string | null>(null);
  const [digits, setDigits] = useState(["", "", "", "", "", ""]);
  const [backupCode, setBackupCode] = useState("");
  const digitRefs = useRef<(HTMLInputElement | null)[]>([]);
  const [error, setError] = useState<string | null>(
    expired ? "Session expired. Please sign in again." :
    resetDone ? "Password reset. Sign in with your new password." : null
  );
  const [loading, setLoading] = useState(false);
  const [lockMinutes, setLockMinutes] = useState(0);

  useEffect(() => {
    api.get("/api/auth/capabilities")
      .then((data) => setCaps(data))
      .catch(() => {});
  }, []);

  async function handleLogin(e?: React.FormEvent) {
    e?.preventDefault();
    const emailErr = validateEmail(email);
    if (emailErr) { setError(emailErr); return; }
    const passErr = validatePassword(password);
    if (passErr) { setError(passErr); return; }

    setLoading(true);
    setError(null);
    try {
      const res = await api.post("/api/auth/login", { email, password });
      if (res.mfa_required) {
        setMfaToken(res.mfa_token);
        setStep("mfa");
        setTimeout(() => digitRefs.current[0]?.focus(), 80);
      } else if (res.force_password_change) {
        router.push("/change-password?forced=true");
      } else {
        router.push(redirect);
      }
    } catch (e: unknown) {
      const err = e as { status?: number; message?: string };
      if (err.status === 423) {
        const mins = parseInt(err.message?.match(/(\d+)/)?.[1] || "15", 10);
        setLockMinutes(mins);
        setError(`Account locked. Try again in ${mins} minutes.`);
      } else {
        setError(err.message || "Login failed");
      }
    } finally {
      setLoading(false);
    }
  }

  async function handleMfa(code: string) {
    setLoading(true);
    setError(null);
    try {
      const res = await api.post("/api/auth/login", { mfa_token: mfaToken, code });
      if (res.force_password_change) {
        router.push("/change-password?forced=true");
      } else {
        router.push(redirect);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Invalid code");
      setDigits(["", "", "", "", "", ""]);
      digitRefs.current[0]?.focus();
    } finally {
      setLoading(false);
    }
  }

  const onDigit = (i: number, v: string) => {
    if (!/^\d?$/.test(v)) return;
    const next = [...digits];
    next[i] = v;
    setDigits(next);
    if (v && i < 5) digitRefs.current[i + 1]?.focus();
    if (next.every((d) => d) && next.join("").length === 6) {
      handleMfa(next.join(""));
    }
  };
  const onDigitKey = (i: number, e: React.KeyboardEvent) => {
    if (e.key === "Backspace" && !digits[i] && i > 0) digitRefs.current[i - 1]?.focus();
  };

  return (
    <div className="login-screen">
      <button className="btn icon ghost" onClick={toggleTheme} aria-label="Toggle theme"
        style={{ position: "absolute", top: 20, right: 20, zIndex: 2 }}>
        {themeMode === "dark" ? <Icons.Sun size={16} /> : <Icons.Moon size={16} />}
      </button>
      <div className="login-card">
        <div className="login-mark">
          <div className="brand-mark" style={{ width: 36, height: 36 }}>
            <StrathonLogo size={36} />
          </div>
          <span style={{ fontSize: 22, fontWeight: 600, letterSpacing: "-0.02em" }}>Strathon</span>
        </div>

        {step === "creds" && (
          <form onSubmit={handleLogin}>
            <h1 className="t-h2" style={{ marginBottom: 6 }}>Sign in</h1>
            <p className="t-sm text-secondary" style={{ marginBottom: 22 }}>to your AI agent firewall workspace</p>
            <div className="form-row">
              <label className="form-label">Email</label>
              <input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" autoFocus autoComplete="email" />
            </div>
            <div className="form-row">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                <label className="form-label">Password</label>
                <a href="/forgot-password" className="t-sm" style={{ color: "var(--accent)" }}>Forgot?</a>
              </div>
              <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
            </div>
            {error && (
              <div className="t-sm" style={{ color: resetDone ? "var(--success)" : "var(--danger)", marginBottom: 10 }}>
                {error}
              </div>
            )}
            <button className="btn primary" style={{ width: "100%", height: 38 }} type="submit" disabled={loading || lockMinutes > 0}>
              {loading ? "Signing in\u2026" : "Sign in"}
            </button>
            {caps?.registration_enabled && (
              <div className="t-sm text-muted" style={{ marginTop: 18, textAlign: "center" }}>
                Don&apos;t have an account? <a href="/register" style={{ color: "var(--accent)" }}>Register</a>
              </div>
            )}
          </form>
        )}

        {step === "mfa" && (
          <div>
            <h1 className="t-h2" style={{ marginBottom: 6 }}>Two-factor</h1>
            <p className="t-sm text-secondary" style={{ marginBottom: 22 }}>Enter the 6-digit code from your authenticator app.</p>
            <div className="mfa-digits" style={{ marginBottom: 18, justifyContent: "center" }}>
              {digits.map((d, i) => (
                <input key={i} ref={(el) => { digitRefs.current[i] = el; }} className="mfa-digit" inputMode="numeric" maxLength={1} value={d} onChange={(e) => onDigit(i, e.target.value)} onKeyDown={(e) => onDigitKey(i, e)} disabled={loading} />
              ))}
            </div>
            {error && <div className="t-sm" style={{ color: "var(--danger)", marginBottom: 10, textAlign: "center" }}>{error}</div>}
            <button className="btn ghost" style={{ width: "100%" }} onClick={() => { setStep("creds"); setError(null); }} disabled={loading}><Icons.ChevronLeft size={13} /> Back</button>
            <div className="t-sm text-muted" style={{ marginTop: 14, textAlign: "center" }}>
              Lost device? <button style={{ color: "var(--accent)", background: "none", border: "none", cursor: "pointer", fontSize: "inherit" }} onClick={() => { setStep("backup"); setError(null); }}>Use a backup code</button>
            </div>
          </div>
        )}

        {step === "backup" && (
          <div>
            <h1 className="t-h2" style={{ marginBottom: 6 }}>Backup code</h1>
            <p className="t-sm text-secondary" style={{ marginBottom: 22 }}>Enter one of your backup codes.</p>
            <div className="form-row">
              <input className="input" value={backupCode} onChange={(e) => setBackupCode(e.target.value)} placeholder="xxxx-xxxx-xxxx" autoFocus style={{ fontFamily: "var(--font-mono)", textAlign: "center", letterSpacing: "0.05em" }} />
            </div>
            {error && <div className="t-sm" style={{ color: "var(--danger)", marginBottom: 10, textAlign: "center" }}>{error}</div>}
            <button className="btn primary" style={{ width: "100%", height: 38 }} disabled={loading || !backupCode.trim()} onClick={() => handleMfa(backupCode.trim())}>
              {loading ? "Verifying\u2026" : "Verify"}
            </button>
            <button className="btn ghost" style={{ width: "100%", marginTop: 8 }} onClick={() => { setStep("mfa"); setError(null); }} disabled={loading}><Icons.ChevronLeft size={13} /> Back to TOTP</button>
          </div>
        )}
      </div>
    </div>
  );
}

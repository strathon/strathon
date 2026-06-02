"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { Icons } from "@/components/icons";
import { StrathonLogo } from "@/components/logo";
import { api } from "@/lib/api-client";
import { setTheme, getStoredTheme, resolveTheme } from "@/lib/theme";
import { validateEmail, validatePassword, passwordRules } from "@/lib/validation";

export default function RegisterPage() {
  const router = useRouter();
  const [themeMode, setThemeMode] = useState<"light" | "dark">("dark");
  useEffect(() => { setThemeMode(resolveTheme(getStoredTheme())); }, []);
  function toggleTheme() {
    const next = themeMode === "dark" ? "light" : "dark";
    setTheme(next);
    setThemeMode(next);
  }
  const [capsLoaded, setCapsLoaded] = useState(false);

  useEffect(() => {
    api.get("/api/auth/capabilities").then((caps) => {
      if (!caps?.registration_enabled) router.replace("/login");
      else setCapsLoaded(true);
    }).catch(() => setCapsLoaded(true));
  }, [router]);

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) { setError("Display name is required"); return; }
    const emailErr = validateEmail(email);
    if (emailErr) { setError(emailErr); return; }
    const passErr = validatePassword(password);
    if (passErr) { setError(passErr); return; }
    if (password !== confirm) { setError("Passwords do not match"); return; }

    setLoading(true);
    setError(null);
    try {
      await api.post("/api/auth/register", { display_name: name, email, password });
      router.push("/overview?welcome=true");
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Registration failed";
      setError(msg.includes("exists") ? "Account already exists. Log in instead." : msg);
    } finally {
      setLoading(false);
    }
  }

  if (!capsLoaded) return <div className="login-screen"><div className="login-card" style={{ textAlign: "center" }}><span className="spinner" /></div></div>;

  return (
    <div className="login-screen">
      <button className="btn icon ghost" onClick={toggleTheme} aria-label="Toggle theme"
        style={{ position: "absolute", top: 20, right: 20, zIndex: 2 }}>
        {themeMode === "dark" ? <Icons.Sun size={16} /> : <Icons.Moon size={16} />}
      </button>
      <div className="login-card">
        <div className="login-mark">
          <div className="brand-mark" style={{ width: 36, height: 36 }}><StrathonLogo size={36} /></div>
          <span style={{ fontSize: 22, fontWeight: 600, letterSpacing: "-0.02em" }}>Strathon</span>
        </div>
        <form onSubmit={handleSubmit}>
          <h1 className="t-h2" style={{ marginBottom: 6 }}>Create account</h1>
          <p className="t-sm text-secondary" style={{ marginBottom: 22 }}>Set up your AI agent firewall workspace</p>
          <div className="form-row">
            <label className="form-label">Display name</label>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Your name" autoFocus autoComplete="name" />
          </div>
          <div className="form-row">
            <label className="form-label">Email</label>
            <input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" autoComplete="email" />
          </div>
          <div className="form-row">
            <label className="form-label">Password</label>
            <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="At least 8 characters" autoComplete="new-password" />
            {password.length > 0 && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 14px", marginTop: 8 }}>
                {passwordRules(password).map((r) => (
                  <span key={r.label} className="t-sm" style={{ display: "inline-flex", alignItems: "center", gap: 5, color: r.met ? "var(--success)" : "var(--text-muted)" }}>
                    <Icons.Check size={12} style={{ opacity: r.met ? 1 : 0.35 }} /> {r.label}
                  </span>
                ))}
              </div>
            )}
          </div>
          <div className="form-row">
            <label className="form-label">Confirm password</label>
            <input className="input" type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" />
          </div>
          {error && (
            <div className="t-sm" style={{ color: "var(--danger)", marginBottom: 10 }}>
              {error}
              {error.includes("Log in") && <> <a href="/login" style={{ color: "var(--accent)" }}>Log in</a></>}
            </div>
          )}
          <button className="btn primary" style={{ width: "100%", height: 38 }} type="submit" disabled={loading}>
            {loading ? "Creating account\u2026" : "Create account"}
          </button>
          <div className="t-sm text-muted" style={{ marginTop: 18, textAlign: "center" }}>
            Already have an account? <a href="/login" style={{ color: "var(--accent)" }}>Sign in</a>
          </div>
        </form>
      </div>
    </div>
  );
}

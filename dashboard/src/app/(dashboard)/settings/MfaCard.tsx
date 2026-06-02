"use client";

import { useState, useEffect, useRef } from "react";
import QRCode from "qrcode";
import { Icons } from "@/components/icons";
import { Badge, Sheet, Modal, useToast } from "@/components/ui";
import { api } from "@/lib/api-client";
import { useUser } from "@/lib/user-context";

type Step = "intro" | "scan" | "verify" | "backup";

/**
 * Multi-factor authentication card for the current user. Enrollment is a
 * three-step flow that mirrors the receiver's contract:
 *   POST /api/auth/mfa?action=setup        -> { secret, otpauth_uri }
 *   POST /api/auth/mfa?action=verify-setup -> { backup_codes }   (body: { code })
 *   POST /api/auth/mfa?action=disable      (body: { password, code })
 * The QR is rendered locally from otpauth_uri (no network call — the URI
 * contains the TOTP secret and must never leave the browser).
 */
export function MfaCard() {
  const { user, refetch } = useUser();
  const toast = useToast();
  const enabled = !!user?.mfa_enabled;

  const [step, setStep] = useState<Step>("intro");
  const [open, setOpen] = useState(false);
  const [secret, setSecret] = useState("");
  const [otpauthUri, setOtpauthUri] = useState("");
  const [qrSvg, setQrSvg] = useState("");
  const [code, setCode] = useState("");
  const [backupCodes, setBackupCodes] = useState<string[]>([]);
  const [savedConfirmed, setSavedConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Disable flow
  const [disableOpen, setDisableOpen] = useState(false);
  const [disablePassword, setDisablePassword] = useState("");
  const [disableCode, setDisableCode] = useState("");
  const [disableBusy, setDisableBusy] = useState(false);
  const [disableError, setDisableError] = useState<string | null>(null);

  // Render the QR locally whenever we get a new otpauth URI.
  useEffect(() => {
    if (!otpauthUri) { setQrSvg(""); return; }
    let cancelled = false;
    QRCode.toString(otpauthUri, { type: "svg", margin: 1, width: 200 })
      .then((svg) => { if (!cancelled) setQrSvg(svg); })
      .catch(() => { if (!cancelled) setQrSvg(""); });
    return () => { cancelled = true; };
  }, [otpauthUri]);

  function reset() {
    setStep("intro"); setSecret(""); setOtpauthUri(""); setQrSvg("");
    setCode(""); setBackupCodes([]); setSavedConfirmed(false); setError(null);
  }

  async function startSetup() {
    setBusy(true); setError(null);
    try {
      const res = await api.post("/api/auth/mfa?action=setup", {});
      setSecret(res.secret); setOtpauthUri(res.otpauth_uri);
      setStep("scan"); setOpen(true);
    } catch (e) {
      toast.push({ tone: "danger", title: "Couldn't start MFA setup", body: e instanceof Error ? e.message : "Try again" });
    } finally { setBusy(false); }
  }

  async function verify() {
    if (code.replace(/\s/g, "").length !== 6) { setError("Enter the 6-digit code from your app."); return; }
    setBusy(true); setError(null);
    try {
      const res = await api.post("/api/auth/mfa?action=verify-setup", { code: code.replace(/\s/g, "") });
      setBackupCodes(res.backup_codes || []);
      setStep("backup");
    } catch {
      setError("That code didn't match. Check your authenticator app and try again.");
    } finally { setBusy(false); }
  }

  async function finish() {
    setOpen(false);
    await refetch();
    toast.push({ tone: "success", title: "Two-factor authentication enabled" });
    reset();
  }

  async function doDisable() {
    if (!disablePassword) { setDisableError("Enter your password."); return; }
    if (disableCode.replace(/\s/g, "").length !== 6) { setDisableError("Enter the 6-digit code from your app."); return; }
    setDisableBusy(true); setDisableError(null);
    try {
      await api.post("/api/auth/mfa?action=disable", { password: disablePassword, code: disableCode.replace(/\s/g, "") });
      setDisableOpen(false); setDisablePassword(""); setDisableCode("");
      await refetch();
      toast.push({ tone: "success", title: "Two-factor authentication disabled" });
    } catch {
      setDisableError("Couldn't disable MFA. Check your password and code.");
    } finally { setDisableBusy(false); }
  }

  function copyBackup() {
    navigator.clipboard?.writeText(backupCodes.join("\n")).then(
      () => toast.push({ tone: "success", title: "Backup codes copied" }),
      () => toast.push({ tone: "danger", title: "Copy failed" }),
    );
  }

  function downloadBackup() {
    const header = "Strathon MFA backup codes\nEach code works once. Keep these somewhere safe.\n\n";
    const blob = new Blob([header + backupCodes.join("\n") + "\n"], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "strathon-backup-codes.txt";
    a.click();
    URL.revokeObjectURL(url);
    toast.push({ tone: "success", title: "Backup codes downloaded" });
  }

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div>
          <div style={{ fontWeight: 600, display: "flex", alignItems: "center", gap: 8 }}>
            Multi-factor authentication
            {enabled ? <Badge kind="success" dot>enabled</Badge> : <Badge kind="muted">disabled</Badge>}
          </div>
          <div className="t-sm text-secondary" style={{ marginTop: 4 }}>
            {enabled ? "Protected with a TOTP authenticator app." : "Add an extra layer of security with an authenticator app."}
          </div>
        </div>
        {enabled ? (
          <button className="btn" onClick={() => { setDisableError(null); setDisablePassword(""); setDisableCode(""); setDisableOpen(true); }}>Manage</button>
        ) : (
          <button className="btn primary" onClick={startSetup} disabled={busy}>
            {busy ? "Starting\u2026" : <><Icons.Shield size={13} /> Enable MFA</>}
          </button>
        )}
      </div>

      {/* Enrollment sheet */}
      <Sheet open={open} onClose={() => { setOpen(false); reset(); }} eyebrow="Security" title="Set up two-factor authentication">
        {step === "scan" && (
          <div className="col" style={{ gap: 16 }}>
            <p className="t-sm text-secondary">Scan this QR code with an authenticator app (Google Authenticator, 1Password, Authy), or enter the key manually.</p>
            <div style={{ display: "grid", placeItems: "center", padding: 16, background: "#fff", borderRadius: 12, width: "fit-content", margin: "0 auto" }}
              dangerouslySetInnerHTML={{ __html: qrSvg }} />
            <div>
              <div className="form-label">Manual entry key</div>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <code style={{ flex: 1, fontFamily: "var(--font-mono)", fontSize: 13, padding: "8px 10px", background: "var(--bg-input)", borderRadius: 8, wordBreak: "break-all" }}>{secret}</code>
                <button className="btn ghost" onClick={() => { navigator.clipboard?.writeText(secret); toast.push({ tone: "success", title: "Key copied" }); }}><Icons.Copy size={13} /></button>
              </div>
            </div>
            <button className="btn primary" onClick={() => { setStep("verify"); setError(null); }}>Next: enter a code</button>
          </div>
        )}

        {step === "verify" && (
          <div className="col" style={{ gap: 16 }}>
            <p className="t-sm text-secondary">Enter the 6-digit code shown in your authenticator app to confirm setup.</p>
            <input className="input mono" value={code} onChange={(e) => setCode(e.target.value.replace(/[^\d]/g, "").slice(0, 6))}
              placeholder="000000" autoFocus inputMode="numeric"
              style={{ fontFamily: "var(--font-mono)", fontSize: 20, letterSpacing: "0.3em", textAlign: "center" }} />
            {error && <div className="t-sm" style={{ color: "var(--danger)" }}>{error}</div>}
            <div style={{ display: "flex", gap: 8 }}>
              <button className="btn ghost" onClick={() => { setStep("scan"); setError(null); }}>Back</button>
              <button className="btn primary" style={{ flex: 1 }} onClick={verify} disabled={busy}>{busy ? "Verifying\u2026" : "Verify & enable"}</button>
            </div>
          </div>
        )}

        {step === "backup" && (
          <div className="col" style={{ gap: 16 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--success)" }}>
              <Icons.ShieldCheck size={18} /> <strong>MFA is on. Save your backup codes.</strong>
            </div>
            <p className="t-sm text-secondary">Each code works once if you lose access to your authenticator. Store them somewhere safe — they won&apos;t be shown again.</p>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, padding: 16, background: "var(--bg-input)", borderRadius: 10, fontFamily: "var(--font-mono)", fontSize: 14 }}>
              {backupCodes.map((c) => <div key={c} style={{ textAlign: "center" }}>{c}</div>)}
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button className="btn ghost" style={{ flex: 1 }} onClick={copyBackup}><Icons.Copy size={13} /> Copy codes</button>
              <button className="btn ghost" style={{ flex: 1 }} onClick={downloadBackup}><Icons.Download size={13} /> Download .txt</button>
            </div>
            <label style={{ display: "flex", gap: 8, alignItems: "center", cursor: "pointer" }}>
              <input type="checkbox" checked={savedConfirmed} onChange={(e) => setSavedConfirmed(e.target.checked)} />
              <span className="t-sm">I&apos;ve saved these codes somewhere safe</span>
            </label>
            <button className="btn primary" onClick={finish} disabled={!savedConfirmed}>Done</button>
          </div>
        )}
      </Sheet>

      {/* Disable sheet */}
      <Sheet open={disableOpen} onClose={() => setDisableOpen(false)} eyebrow="Security" title="Manage two-factor authentication">
        <div className="col" style={{ gap: 16 }}>
          <p className="t-sm text-secondary">To turn off MFA, confirm your password and a current authenticator code.</p>
          <div>
            <div className="form-label">Password</div>
            <input className="input" type="password" value={disablePassword} onChange={(e) => setDisablePassword(e.target.value)} autoComplete="current-password" />
          </div>
          <div>
            <div className="form-label">Authenticator code</div>
            <input className="input mono" value={disableCode} onChange={(e) => setDisableCode(e.target.value.replace(/[^\d]/g, "").slice(0, 6))}
              placeholder="000000" inputMode="numeric"
              style={{ fontFamily: "var(--font-mono)", letterSpacing: "0.2em", textAlign: "center" }} />
          </div>
          {disableError && <div className="t-sm" style={{ color: "var(--danger)" }}>{disableError}</div>}
          <button className="btn danger" onClick={doDisable} disabled={disableBusy}>{disableBusy ? "Disabling\u2026" : "Disable MFA"}</button>
        </div>
      </Sheet>
    </div>
  );
}

"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Icons } from "@/components/icons";
import { Badge, Dropdown, Empty, SkeletonCards, useToast } from "@/components/ui";
import { useApi, api } from "@/lib/api-client";
import { usePermissions } from "@/lib/permissions";
import { validatePolicyName, validateCEL } from "@/lib/validation";

const ACTION_COLOR: Record<string, string> = { block: "danger", steer: "warning", throttle: "warning", log: "muted", alert: "info", require_approval: "info" };

interface Template {
  id: string;
  name: string;
  description: string;
  owasp_risks?: string[];
  action: string;
  match_expression: string;
  action_config?: Record<string, unknown> | null;
  tags?: string[];
}

export default function NewPolicyPage() {
  const router = useRouter();
  const toast = useToast();
  const perms = usePermissions();

  // Two-step flow: pick a template (or start blank), then edit.
  const [step, setStep] = useState<"gallery" | "editor">("gallery");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [cel, setCel] = useState("");
  const [action, setAction] = useState("block");
  const [actionConfig, setActionConfig] = useState<Record<string, unknown> | null>(null);
  const [status, setStatus] = useState("shadow");
  const [priority, setPriority] = useState(100);
  const [saving, setSaving] = useState(false);

  const { data: tmplData, loading: tmplLoading, error: tmplError, refetch } = useApi<{ data: Template[] }>("/api/policies/templates");
  const templates = tmplData?.data || [];

  function startFromTemplate(t: Template) {
    setName(t.id);
    setDescription(t.description || "");
    setCel(t.match_expression || "");
    setAction(t.action || "block");
    setActionConfig(t.action_config ?? null);
    setStatus("shadow");
    setPriority(100);
    setStep("editor");
  }

  function startFromScratch() {
    setName("");
    setDescription("");
    setCel("// CEL expression — return true to match\nattrs[\"gen_ai.tool.name\"] == \"shell_exec\"");
    setAction("block");
    setActionConfig(null);
    setStatus("shadow");
    setPriority(100);
    setStep("editor");
  }

  async function handleCreate() {
    const nameErr = validatePolicyName(name);
    if (nameErr) { toast.push({ tone: "danger", title: nameErr }); return; }
    const celErr = validateCEL(cel);
    if (celErr) { toast.push({ tone: "danger", title: celErr }); return; }
    setSaving(true);
    try {
      // The BFF maps { cel, status } to the receiver's { match_expression,
      // enabled, shadow } contract.
      const res = await api.post("/api/policies", { name, description, cel, action, status, priority, action_config: actionConfig });
      void res;
      toast.push({ tone: "success", title: "Policy created", body: name });
      router.push("/policies");
    } catch (e) {
      toast.push({ tone: "danger", title: "Failed to create", body: e instanceof Error ? e.message : "Unknown error" });
    } finally {
      setSaving(false);
    }
  }

  // Viewers can't create policies; send them back.
  if (!perms.canWritePolicies) {
    return (
      <div className="page">
        <div className="card" style={{ padding: 24, textAlign: "center" }}>
          <Empty icon={<Icons.Lock size={24} />} title="You don't have permission to create policies"
            subtitle="Creating and editing policies requires an operator, admin, or owner role."
            action={<button className="btn" onClick={() => router.push("/policies")}>Back to policies</button>} />
        </div>
      </div>
    );
  }

  if (step === "gallery") {
    return (
      <div className="page" style={{ maxWidth: 980 }}>
        <div className="page-header">
          <div>
            <div className="t-caption text-muted" style={{ marginBottom: 4 }}>New policy</div>
            <h1 className="t-h2">Start from a template</h1>
            <div className="page-subtitle">OWASP-mapped policies you can use as-is or tailor. Or start from a blank CEL rule.</div>
          </div>
          <button className="btn ghost" onClick={() => router.push("/policies")}>Cancel</button>
        </div>

        <button className="card" onClick={startFromScratch}
          style={{ width: "100%", textAlign: "left", cursor: "pointer", display: "flex", alignItems: "center", gap: 12, marginBottom: 16 }}>
          <div style={{ width: 36, height: 36, borderRadius: 8, background: "var(--bg-input)", display: "grid", placeItems: "center" }}><Icons.Plus size={16} /></div>
          <div>
            <div style={{ fontWeight: 600 }}>Start from scratch</div>
            <div className="t-sm text-muted">Open a blank CEL editor and write your own rule.</div>
          </div>
        </button>

        {tmplLoading ? <SkeletonCards count={6} /> : tmplError ? (
          <div className="card" style={{ padding: 24, textAlign: "center" }}>
            <div style={{ color: "var(--danger)", marginBottom: 8 }}>{tmplError}</div>
            <button className="btn" onClick={refetch}>Retry</button>
          </div>
        ) : templates.length === 0 ? (
          <Empty icon={<Icons.Shield size={24} />} title="No templates available" subtitle="Start from scratch to write your own CEL rule."
            action={<button className="btn primary" onClick={startFromScratch}><Icons.Plus size={13} /> Blank policy</button>} />
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 12 }}>
            {templates.map((t) => (
              <button key={t.id} className="card" onClick={() => startFromTemplate(t)}
                style={{ textAlign: "left", cursor: "pointer", display: "flex", flexDirection: "column", gap: 8, alignItems: "stretch" }}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                  <span style={{ fontWeight: 600, fontSize: 14 }}>{t.name}</span>
                  <Badge kind={ACTION_COLOR[t.action] || "muted"} mono>{t.action}</Badge>
                </div>
                <div className="t-sm text-secondary" style={{ lineHeight: 1.45, flex: 1 }}>{t.description}</div>
                {t.owasp_risks && t.owasp_risks.length > 0 && (
                  <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                    {t.owasp_risks.map((r) => <Badge key={r} kind="info">{r}</Badge>)}
                  </div>
                )}
              </button>
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="page" style={{ maxWidth: 860 }}>
      <div className="page-header">
        <div>
          <div className="t-caption text-muted" style={{ marginBottom: 4, display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }} onClick={() => setStep("gallery")}>
            <Icons.ChevronLeft size={12} /> Templates
          </div>
          <h1 className="t-h2">Create a policy</h1>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn ghost" onClick={() => router.push("/policies")}>Cancel</button>
          <button className="btn primary" onClick={handleCreate} disabled={saving || !name.trim()}>
            {saving ? "Creating\u2026" : <><Icons.Save size={13} /> Create policy</>}
          </button>
        </div>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <div className="card">
          <div className="card-header"><span className="card-title">Basics</span></div>
          <div className="col" style={{ gap: 12 }}>
            <div>
              <div className="form-label">Name <span className="text-muted">(kebab-case)</span></div>
              <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. block-prompt-injection" autoFocus />
            </div>
            <div>
              <div className="form-label">Description</div>
              <textarea className="textarea" rows={2} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What does this policy enforce?" />
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-header"><span className="card-title">CEL expression</span></div>
          <div style={{ padding: "12px 16px", background: "var(--bg-input)", borderRadius: 8, minHeight: 120 }}>
            <textarea className="textarea mono" rows={6} value={cel} onChange={(e) => setCel(e.target.value)} style={{ width: "100%", background: "transparent", border: "none", fontFamily: "var(--font-mono)", fontSize: 13, resize: "vertical" }} />
          </div>
        </div>

        <div className="card">
          <div className="card-header"><span className="card-title">Configuration</span></div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 16 }}>
            <div>
              <div className="form-label">Action</div>
              <Dropdown width={220} trigger={({ toggle }) => (
                <button className="btn" style={{ width: "100%", justifyContent: "space-between" }} onClick={toggle}>
                  <Badge kind={ACTION_COLOR[action] || "muted"} mono>{action}</Badge><Icons.ChevronDown size={13} />
                </button>
              )} items={(["block", "steer", "throttle", "log", "alert", "require_approval"] as const).map((a) => ({ label: a, onClick: () => setAction(a) }))} />
            </div>
            <div>
              <div className="form-label">Initial status</div>
              <Dropdown width={180} trigger={({ toggle }) => (
                <button className="btn" style={{ width: "100%", justifyContent: "space-between" }} onClick={toggle}>
                  <span style={{ textTransform: "capitalize" }}>{status}</span><Icons.ChevronDown size={13} />
                </button>
              )} items={[
                { label: "Enabled", onClick: () => setStatus("enabled") },
                { label: "Shadow (test mode)", onClick: () => setStatus("shadow") },
                { label: "Disabled", onClick: () => setStatus("disabled") },
              ]} />
            </div>
            <div>
              <div className="form-label">Priority <span className="text-muted">(higher runs first)</span></div>
              <input className="input" type="number" value={priority} onChange={(e) => setPriority(Number(e.target.value))} />
            </div>
          </div>
        </div>

        <div className="t-sm text-muted" style={{ padding: "8px 0" }}>
          Tip: start with <strong>shadow</strong> status to test your policy against live traffic without blocking anything. Switch to <strong>enabled</strong> when you&apos;re confident.
        </div>
      </div>
    </div>
  );
}

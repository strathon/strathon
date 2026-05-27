"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Icons } from "@/components/icons";
import { Badge, Dropdown, HighlightedCEL, useToast } from "@/components/ui";
import { api } from "@/lib/api-client";
import { validatePolicyName, validateCEL } from "@/lib/validation";

const ACTION_COLOR: Record<string, string> = { block: "danger", steer: "warning", throttle: "warning", log: "muted", alert: "info", require_approval: "info" };

export default function NewPolicyPage() {
  const router = useRouter();
  const toast = useToast();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [cel, setCel] = useState('// Your CEL expression\nrequest.input_risk.injection_score > 0.8');
  const [action, setAction] = useState("block");
  const [status, setStatus] = useState("shadow");
  const [priority, setPriority] = useState(100);
  const [saving, setSaving] = useState(false);

  async function handleCreate() {
    const nameErr = validatePolicyName(name);
    if (nameErr) { toast.push({ tone: "danger", title: nameErr }); return; }
    const celErr = validateCEL(cel);
    if (celErr) { toast.push({ tone: "danger", title: celErr }); return; }
    setSaving(true);
    try {
      const res = await api.post("/api/policies", { name, description, cel, action, status, priority });
      const newId = res?.data?.id || res?.id;
      toast.push({ tone: "success", title: "Policy created", body: name });
      router.push(newId ? `/policies/${newId}` : "/policies");
    } catch (e) {
      toast.push({ tone: "danger", title: "Failed to create", body: e instanceof Error ? e.message : "Unknown error" });
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="page" style={{ maxWidth: 860 }}>
      <div className="page-header">
        <div>
          <div className="t-caption text-muted" style={{ marginBottom: 4 }}>New policy</div>
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

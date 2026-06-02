"use client";

import { useState } from "react";
import { Icons } from "./icons";
import { Dropdown, Modal, useToast } from "./ui";
import { useUser } from "@/lib/user-context";
import { api } from "@/lib/api-client";

/**
 * Workspace/project switcher shown under the brand in the sidebar.
 * Lists every project the user is a member of, switches the active project
 * (sets the project cookie via the BFF, then reloads so all data refetches
 * under the new project context), and offers project creation.
 */
export function ProjectSwitcher({ collapsed }: { collapsed?: boolean }) {
  const { user, projects, refetch } = useUser();
  const toast = useToast();
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [creating, setCreating] = useState(false);
  const [switching, setSwitching] = useState(false);

  const currentId = user?.project_id || null;
  const currentName = user?.project_name || "Project";

  async function switchTo(projectId: string) {
    if (projectId === currentId || switching) return;
    setSwitching(true);
    try {
      await api.post("/api/projects/switch", { project_id: projectId });
      // Full reload: every page's data is project-scoped, so reload under
      // the new context rather than trying to refetch each view.
      window.location.href = "/";
    } catch (e) {
      toast.push({ tone: "danger", title: e instanceof Error ? e.message : "Failed to switch project" });
      setSwitching(false);
    }
  }

  // Auto-suggest a slug from the name (lowercase, hyphenated).
  function onNameChange(v: string) {
    setName(v);
    setSlug(v.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64));
  }

  async function createProject() {
    if (creating) return;
    if (!name.trim()) { toast.push({ tone: "danger", title: "Name is required" }); return; }
    if (!/^[a-z0-9]([a-z0-9-]{1,62}[a-z0-9])?$/.test(slug)) {
      toast.push({ tone: "danger", title: "Slug must be 3-64 lowercase letters, numbers, or hyphens" });
      return;
    }
    setCreating(true);
    try {
      await api.post("/api/projects", { name: name.trim(), slug });
      toast.push({ tone: "success", title: "Project created", body: name.trim() });
      // The BFF switched the cookie to the new project; reload into it.
      window.location.href = "/";
    } catch (e) {
      toast.push({ tone: "danger", title: e instanceof Error ? e.message : "Failed to create project" });
      setCreating(false);
    }
  }

  const items = [
    ...projects.map((p) => ({
      icon: p.id === currentId ? <Icons.Check size={13} /> : <span style={{ width: 13, display: "inline-block" }} />,
      label: p.name,
      onClick: () => switchTo(p.id),
    })),
    { divider: true },
    { icon: <Icons.Plus size={13} />, label: "New project", onClick: () => { setName(""); setSlug(""); setShowCreate(true); } },
  ];

  return (
    <>
      <Dropdown
        align="left"
        side="right"
        width={220}
        items={items}
        trigger={({ toggle }) => (
          <button className="project-switcher" onClick={toggle} title={collapsed ? currentName : undefined}>
            <Icons.Layers size={14} className="project-switcher-icon" />
            <span className="project-switcher-name">{currentName}</span>
            <Icons.ChevronDown size={13} className="project-switcher-chevron" />
          </button>
        )}
      />
      <Modal open={showCreate} onClose={() => setShowCreate(false)} title="New project"
        confirmLabel={creating ? "Creating…" : "Create project"} onConfirm={createProject}
        body={
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <span className="t-sm text-muted">Name</span>
              <input className="input" value={name} onChange={(e) => onNameChange(e.target.value)} placeholder="Production" autoFocus />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <span className="t-sm text-muted">Slug</span>
              <input className="input mono" value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="production" />
              <span className="t-sm text-muted">Used in URLs and API. Lowercase letters, numbers, hyphens.</span>
            </label>
          </div>
        } />
    </>
  );
}

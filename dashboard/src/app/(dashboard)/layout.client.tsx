"use client";

import { useState, useEffect, useMemo, useCallback, Suspense } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Sidebar, Header, UserMenu, CommandPalette, MobileNav } from "@/components/shell";
import { ToastProvider, ShortcutHelp, Empty, Modal, useToast } from "@/components/ui";
import { Icons } from "@/components/icons";
import { useApi, api } from "@/lib/api-client";
import { UserProvider, useUser } from "@/lib/user-context";
import { usePermissions } from "@/lib/permissions";
import { setTheme, getStoredTheme, watchSystemTheme } from "@/lib/theme";

const DASHBOARD_VERSION = "1.2.1";

const LABELS: Record<string, string> = {
  overview: "Overview", policies: "Policies", traces: "Traces", spans: "Spans", approvals: "Approvals",
  agents: "Agents", audit: "Audit", budgets: "Budgets", compliance: "Compliance",
  settings: "Settings", apikeys: "API keys",
};

function VersionBanner() {
  const { receiverVersion } = useUser();
  const [dismissed, setDismissed] = useState(false);
  if (dismissed || !receiverVersion || receiverVersion.version === DASHBOARD_VERSION) return null;
  return (
    <div style={{ background: "var(--warning-bg)", borderBottom: "1px solid var(--warning-border)", padding: "8px 16px", display: "flex", alignItems: "center", justifyContent: "center", gap: 8, fontSize: 13 }}>
      <span style={{ color: "var(--warning)" }}>&#9888;</span>
      <span>Dashboard v{DASHBOARD_VERSION} differs from receiver v{receiverVersion.version}. Consider upgrading.</span>
      <button onClick={() => setDismissed(true)} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", padding: 2 }}>&times;</button>
    </div>
  );
}

/**
 * Shown to an authenticated user who is not yet a member of any project.
 * The first registered user becomes the owner of the default project;
 * everyone else needs an owner or admin to add them. Until then there is
 * no project context, so we show this instead of a dashboard whose every
 * API call would fail.
 */
function NoProjectScreen() {
  const { refetch } = useUser();
  const [busy, setBusy] = useState(false);
  const logout = async () => {
    setBusy(true);
    try { await api.post("/api/auth/logout"); } catch {}
    window.location.href = "/login";
  };
  return (
    <div style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 24 }}>
      <div className="card" style={{ maxWidth: 460, width: "100%", textAlign: "center", padding: "32px 28px" }}>
        <Empty
          icon={<Icons.Users size={24} />}
          title="You're not a member of any project yet"
          subtitle="Your account is ready. Ask an owner or admin of your team to add you to a project — once they do, your workspace will appear here."
          action={
            <div style={{ display: "flex", gap: 8, justifyContent: "center" }}>
              <button className="btn" onClick={() => refetch()}>Check again</button>
              <button className="btn ghost" onClick={logout} disabled={busy}>Log out</button>
            </div>
          }
        />
      </div>
    </div>
  );
}

/**
 * The full dashboard chrome, rendered only once we have a user with a
 * project. All hooks run unconditionally (rules of hooks); the render
 * branches on auth/membership state at the end.
 */
function AuthedShell({ children, mode }: { children: React.ReactNode; mode: "self-hosted" | "cloud" }) {
  const { user, loading } = useUser();
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();

  const [collapsed, setCollapsed] = useState(true);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const [cmdOpen, setCmdOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [isMobile, setIsMobile] = useState(false);
  const [haltOpen, setHaltOpen] = useState(false);
  const [halting, setHalting] = useState(false);
  const toast = useToast();
  const perms = usePermissions();

  async function createPanicHalt() {
    setHalting(true);
    try {
      await api.post("/api/halts", { scope: "project", reason: "Manual panic stop from dashboard" });
      toast.push({ tone: "success", title: "Project halted", body: "All agents are stopped. Lift the halt from the budgets/halts view when ready." });
      setHaltOpen(false);
    } catch (e) {
      toast.push({ tone: "danger", title: "Couldn't create halt", body: e instanceof Error ? e.message : "Try again" });
    } finally {
      setHalting(false);
    }
  }

  useEffect(() => {
    const onResize = () => setIsMobile(window.innerWidth < 1024);
    onResize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // Close drawer + scroll top on route change
  useEffect(() => { setMobileOpen(false); setUserMenuOpen(false); window.scrollTo(0, 0); }, [pathname]);

  // Body scroll lock while mobile drawer open
  useEffect(() => {
    if (mobileOpen && isMobile) {
      const prev = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      return () => { document.body.style.overflow = prev || ""; };
    }
  }, [mobileOpen, isMobile]);

  // Keep the page matched to the OS when the theme preference is "system".
  useEffect(() => watchSystemTheme(), []);

  const toggleTheme = useCallback(() => {
    // Toggling picks the opposite of what's on screen and persists it as an
    // explicit choice (so it stops following the OS until changed again).
    const current = document.documentElement.dataset.theme === "light" ? "light" : "dark";
    setTheme(current === "dark" ? "light" : "dark");
  }, []);

  // Keyboard shortcuts
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const inField = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName ?? "");
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setCmdOpen(true); return; }
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key === ",") { e.preventDefault(); router.push("/settings"); return; }
      if ((e.metaKey || e.ctrlKey) && e.key === ".") { e.preventDefault(); e.stopPropagation(); setCollapsed((c) => !c); return; }
      if (e.key === "[" && !inField && !e.metaKey && !e.ctrlKey) { e.preventDefault(); setCollapsed((c) => !c); return; }
      if (e.key === "?" && !inField) { e.preventDefault(); setShortcutsOpen((o) => !o); return; }
      if (e.key === "/" && !inField && !e.metaKey && !e.ctrlKey) {
        const target = document.querySelector<HTMLInputElement>('input.search, input[placeholder*="Search" i], input[placeholder*="Highlight" i]');
        if (target) { e.preventDefault(); target.focus(); target.select?.(); return; }
      }
      if (!inField && !e.metaKey && !e.ctrlKey) {
        const w = window as unknown as { __gprefix?: boolean };
        if (w.__gprefix) {
          const map: Record<string, string> = { o: "overview", p: "policies", t: "traces", s: "spans", a: "approvals", n: "agents", u: "audit", b: "budgets", c: "compliance" };
          if (map[e.key.toLowerCase()]) router.push(`/${map[e.key.toLowerCase()]}`);
          w.__gprefix = false;
        } else if (e.key.toLowerCase() === "g") {
          w.__gprefix = true;
          setTimeout(() => { w.__gprefix = false; }, 800);
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [router]);

  // Breadcrumbs derived from path
  const crumbs = useMemo(() => {
    const segs = pathname.split("/").filter(Boolean);
    const top = segs[0];
    const section = searchParams.get("section");
    if (top === "policies" && segs[1]) return [{ label: "Policies", href: "/policies" }, { label: segs[1] }];
    if (top === "traces" && segs[1]) return [{ label: "Traces", href: "/traces" }, { label: segs[1].slice(0, 12) }];
    if (top === "settings" && section === "export") return [{ label: "Settings", href: "/settings" }, { label: "Export" }];
    if (top === "settings" && section === "apikeys") return [{ label: "Settings", href: "/settings" }, { label: "API keys" }];
    if (top === "apikeys") return [{ label: "Settings", href: "/settings" }, { label: "API keys" }];
    return [{ label: LABELS[top] || top || "Overview" }];
  }, [pathname, searchParams]);

  const hasProject = !!user?.project_id;
  // Only poll approvals once we have a project — otherwise the call has no
  // project context and would fail.
  const { data: approvalsCount } = useApi<{ data: unknown[] }>(hasProject ? "/api/approvals" : null, { status: "pending" });
  const pendingCount = approvalsCount?.data?.length || 0;

  // Still resolving the session: render nothing rather than flash chrome.
  if (loading && !user) return null;
  // Authenticated but no project membership yet.
  if (user && !user.project_id) return <NoProjectScreen />;

  return (
    <>
      <VersionBanner />
      <div className="app" data-collapsed={collapsed} data-mobile-open={mobileOpen}>
        <div className="drawer-backdrop" onClick={() => setMobileOpen(false)} />
        <Sidebar collapsed={collapsed} setCollapsed={setCollapsed} pendingCount={pendingCount} isMobile={isMobile} setMobileOpen={setMobileOpen} onAvatarClick={() => setUserMenuOpen(!userMenuOpen)} />
        <UserMenu open={userMenuOpen} onClose={() => setUserMenuOpen(false)} />
        <div className="main">
          <Header breadcrumbs={crumbs} onOpenCmd={() => setCmdOpen(true)} cmdOpen={cmdOpen} mobileOpen={mobileOpen} setMobileOpen={setMobileOpen} collapsed={collapsed} setCollapsed={setCollapsed} isMobile={isMobile} />
          <div className="page-transition">{children}</div>
        </div>
        {isMobile && <MobileNav pendingCount={pendingCount} />}
      </div>
      <CommandPalette open={cmdOpen} onClose={() => setCmdOpen(false)} toggleTheme={toggleTheme} onAction={(a) => { if (a === "show-shortcuts") setShortcutsOpen(true); else if (a === "create-halt") { if (perms.canWritePolicies) setHaltOpen(true); else toast.push({ tone: "danger", title: "Not allowed", body: "You need operator access or higher to halt agents." }); } }} />
      <ShortcutHelp open={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />
      <Modal open={haltOpen} onClose={() => setHaltOpen(false)} danger title="Halt all agents?"
        confirmLabel={halting ? "Halting\u2026" : "Halt everything"}
        onConfirm={createPanicHalt}
        body={<>This immediately stops every agent in this project from making tool calls. In-flight calls are blocked. You can lift the halt afterward. Use this as a panic stop.</>} />
    </>
  );
}

function DashboardShellInner({ children, mode = "self-hosted" }: { children: React.ReactNode; mode?: "self-hosted" | "cloud" }) {
  return (
    <UserProvider mode={mode}>
      <ToastProvider>
        <AuthedShell mode={mode}>{children}</AuthedShell>
      </ToastProvider>
    </UserProvider>
  );
}

export function DashboardShell({ children, mode }: { children: React.ReactNode; mode?: "self-hosted" | "cloud" }) {
  return <Suspense><DashboardShellInner mode={mode}>{children}</DashboardShellInner></Suspense>;
}

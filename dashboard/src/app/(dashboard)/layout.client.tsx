"use client";

import { useState, useEffect, useMemo, useCallback } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Sidebar, Header, UserMenu, CommandPalette, MobileNav } from "@/components/shell";
import { ToastProvider, ShortcutHelp } from "@/components/ui";
import { useApi } from "@/lib/api-client";
import { UserProvider, useUser } from "@/lib/user-context";

const DASHBOARD_VERSION = "0.1.0";

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

export function DashboardShell({ children, mode = "self-hosted" }: { children: React.ReactNode; mode?: "self-hosted" | "cloud" }) {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();

  const [collapsed, setCollapsed] = useState(true);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const [cmdOpen, setCmdOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [isMobile, setIsMobile] = useState(false);

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

  const toggleTheme = useCallback(() => {
    const root = document.documentElement;
    const cur = root.dataset.theme || "dark";
    root.dataset.theme = cur === "dark" ? "light" : "dark";
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
    if (top === "policies" && segs[1]) {
      
      return [{ label: "Policies", href: "/policies" }, { label: segs[1] }];
    }
    if (top === "traces" && segs[1]) {
      
      return [{ label: "Traces", href: "/traces" }, { label: segs[1].slice(0, 12) }];
    }
    if (top === "settings" && section === "export") return [{ label: "Settings", href: "/settings" }, { label: "Export" }];
    if (top === "settings" && section === "apikeys") return [{ label: "Settings", href: "/settings" }, { label: "API keys" }];
    if (top === "apikeys") return [{ label: "Settings", href: "/settings" }, { label: "API keys" }];
    return [{ label: LABELS[top] || top || "Overview" }];
  }, [pathname, searchParams]);

  const { data: approvalsCount } = useApi<{ data: any[] }>("/api/approvals", { status: "pending" });
  const pendingCount = approvalsCount?.data?.length || 0;

  return (
    <UserProvider mode={mode}>
    <ToastProvider>
      <VersionBanner />
      <div className="app" data-collapsed={collapsed} data-mobile-open={mobileOpen}>
        <div className="drawer-backdrop" onClick={() => setMobileOpen(false)} />
        <Sidebar collapsed={collapsed} setCollapsed={setCollapsed} pendingCount={pendingCount} isMobile={isMobile} setMobileOpen={setMobileOpen} onAvatarClick={() => setUserMenuOpen(!userMenuOpen)} />
        <UserMenu open={userMenuOpen} onClose={() => setUserMenuOpen(false)}  />
        <div className="main">
          <Header breadcrumbs={crumbs} onOpenCmd={() => setCmdOpen(true)} cmdOpen={cmdOpen} mobileOpen={mobileOpen} setMobileOpen={setMobileOpen} collapsed={collapsed} setCollapsed={setCollapsed} isMobile={isMobile} />
          <div className="page-transition">{children}</div>
        </div>
        {isMobile && <MobileNav pendingCount={pendingCount} />}
      </div>
      <CommandPalette open={cmdOpen} onClose={() => setCmdOpen(false)} toggleTheme={toggleTheme} onAction={(a) => { if (a === "show-shortcuts") setShortcutsOpen(true); }} />
      <ShortcutHelp open={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />
    </ToastProvider>
    </UserProvider>
  );
}

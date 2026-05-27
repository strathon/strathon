"use client";

import { useState, useEffect, useRef, useMemo, useCallback } from "react";
import { usePathname, useRouter } from "next/navigation";
import { Icons } from "./icons";
import { Badge, Kbd, Highlight } from "./ui";
import { StrathonLogo } from "./logo";
import { useUser } from "@/lib/user-context";

const COMMAND_ITEMS = [
  { section: "Navigation", icon: "Grid", label: "Go to Overview", to: "overview", kbd: "G O" },
  { section: "Navigation", icon: "Shield", label: "Go to Policies", to: "policies", kbd: "G P" },
  { section: "Navigation", icon: "GitBranch", label: "Go to Traces", to: "traces", kbd: "G T" },
  { section: "Navigation", icon: "Search", label: "Go to Spans", to: "spans", kbd: "G S" },
  { section: "Navigation", icon: "UserCheck", label: "Go to Approvals", to: "approvals", kbd: "G A" },
  { section: "Navigation", icon: "Bot", label: "Go to Agents", to: "agents", kbd: "G N" },
  { section: "Navigation", icon: "ScrollText", label: "Go to Audit", to: "audit", kbd: "G U" },
  { section: "Navigation", icon: "Dollar", label: "Go to Budgets", to: "budgets", kbd: "G B" },
  { section: "Navigation", icon: "FileCheck", label: "Go to Compliance", to: "compliance", kbd: "G C" },
  { section: "Actions", icon: "Plus", label: "Create policy", action: "create-policy", kbd: "C" },
  { section: "Actions", icon: "Zap", label: "Create halt (panic stop)", action: "create-halt", kbd: "H" },
  { section: "Actions", icon: "UserCheck", label: "Approve pending requests", action: "go-approvals" },
  { section: "Actions", icon: "Download", label: "Export data", action: "go-export" },
  { section: "Actions", icon: "Command", label: "Keyboard shortcuts", action: "show-shortcuts", kbd: "?" },
  { section: "Actions", icon: "Moon", label: "Toggle theme", action: "toggle-theme", kbd: "T" },
];

/* ════════════ Nav config ════════════ */
interface NavItem { id: string; label: string; icon: string; badge?: boolean; }
const NAV: NavItem[] = [
  { id: "overview", label: "Overview", icon: "Grid" },
  { id: "policies", label: "Policies", icon: "Shield" },
  { id: "traces", label: "Traces", icon: "GitBranch" },
  { id: "spans", label: "Spans", icon: "Search" },
  { id: "approvals", label: "Approvals", icon: "UserCheck", badge: true },
  { id: "agents", label: "Agents", icon: "Bot" },
  { id: "audit", label: "Audit", icon: "ScrollText" },
  { id: "budgets", label: "Budgets", icon: "Dollar" },
  { id: "compliance", label: "Compliance", icon: "FileCheck" },
];

function useActiveRoute() {
  const pathname = usePathname();
  const seg = pathname.split("/").filter(Boolean)[0] || "overview";
  return seg;
}

/* ════════════ Sidebar ════════════ */
const SIDEBAR_ICONS: (React.ReactNode | null)[] = [
  null,
  <svg key="1" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3l1.9 5.8a2 2 0 001.3 1.3L21 12l-5.8 1.9a2 2 0 00-1.3 1.3L12 21l-1.9-5.8a2 2 0 00-1.3-1.3L3 12l5.8-1.9a2 2 0 001.3-1.3z" /></svg>,
  <svg key="2" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 3a3 3 0 00-3 3v12a3 3 0 003 3 3 3 0 003-3 3 3 0 00-3-3H6a3 3 0 00-3 3 3 3 0 003 3 3 3 0 003-3V6a3 3 0 00-3-3 3 3 0 00-3 3 3 3 0 003 3h12a3 3 0 003-3 3 3 0 00-3-3z" /></svg>,
  <svg key="3" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2L2 7l10 5 10-5-10-5z" /><path d="M2 17l10 5 10-5" /><path d="M2 12l10 5 10-5" /></svg>,
  <svg key="4" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 3v4M3 5h4M6 17v4M4 19h4M13 3l2 2M19.5 8.5l.5.5M17 17l2 2M14 14l7-7" /><path d="M9.5 9.5L3 16v5h5l6.5-6.5" /></svg>,
  <svg key="5" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M17.5 19H9a7 7 0 110-14h8.5" /><polyline points="21 12 17 16" /><polyline points="21 12 17 8" /></svg>,
  <svg key="6" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="4" y1="21" x2="4" y2="14" /><line x1="4" y1="10" x2="4" y2="3" /><line x1="12" y1="21" x2="12" y2="12" /><line x1="12" y1="8" x2="12" y2="3" /><line x1="20" y1="21" x2="20" y2="16" /><line x1="20" y1="12" x2="20" y2="3" /><line x1="1" y1="14" x2="7" y2="14" /><line x1="9" y1="8" x2="15" y2="8" /><line x1="17" y1="16" x2="23" y2="16" /></svg>,
];
const SIDEBAR_BGS = ["#2a2a2a", "#D4819C", "#7BC67E", "#E8A849", "#DB7B7B", "#5BA8C8", "#B8856B"];

export function Sidebar({ collapsed, setCollapsed, pendingCount, isMobile, setMobileOpen, onAvatarClick }: {
  collapsed: boolean; setCollapsed: (v: boolean) => void; pendingCount: number; isMobile: boolean; setMobileOpen: (v: boolean) => void; onAvatarClick: () => void;
}) {
  const route = useActiveRoute();
  const router = useRouter();
  const [avatarIdx, setAvatarIdx] = useState(0);
  const { user: currentUser } = useUser();
  // Default true to match the server's dark-theme assumption; corrected after mount.
  const [isDark, setIsDark] = useState(true);
  useEffect(() => {
    try { const s = parseInt(localStorage.getItem("strathon-avatar-idx") || "0", 10); if (s >= 0 && s < SIDEBAR_BGS.length) setAvatarIdx(s); } catch {}
    setIsDark(document.documentElement.dataset.theme !== "light");
    const handler = (e: Event) => setAvatarIdx((e as CustomEvent).detail);
    const themeObserver = new MutationObserver(() => setIsDark(document.documentElement.dataset.theme !== "light"));
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    window.addEventListener("avatar-changed", handler);
    return () => { window.removeEventListener("avatar-changed", handler); themeObserver.disconnect(); };
  }, []);

  const avBg = avatarIdx === 0 ? (isDark ? "#2a2a2a" : "#e8e6e1") : (SIDEBAR_BGS[avatarIdx] || SIDEBAR_BGS[0]);
  const avColor = avatarIdx === 0 && !isDark ? "#3a3830" : "rgba(255,255,255,0.85)";
  const avBorder = avatarIdx === 0 && !isDark ? "1px solid rgba(0,0,0,0.12)" : "1px solid rgba(255,255,255,0.12)";
  const avIcon = SIDEBAR_ICONS[avatarIdx] || null;

  return (
    <aside className="sidebar">
      <div className="sidebar-top">
        <div className="brand">
          <div className="brand-mark">
            <StrathonLogo size={28} />
          </div>
          <span className="brand-name">Strathon</span>
        </div>
        <button className="sidebar-toggle" data-tooltip="Open sidebar  ⌘."
          onClick={() => isMobile ? setMobileOpen(false) : setCollapsed(!collapsed)}
          title={isMobile ? "Close sidebar" : collapsed ? "Open sidebar  ⌘." : "Close sidebar  ⌘."} aria-label="Toggle sidebar">
          <Icons.PanelLeft size={17} />
        </button>
      </div>
      <nav className="sidebar-nav">
        {NAV.map((it) => {
          const Icon = Icons[it.icon as keyof typeof Icons];
          const active = route === it.id;
          return (
            <button key={it.id} className="nav-item" data-active={active} data-tooltip={it.label}
              onClick={() => router.push(`/${it.id}`)} title={collapsed && !isMobile ? it.label : undefined}>
              <Icon className="nav-icon" />
              <span className="nav-label">{it.label}</span>
              {it.badge && pendingCount > 0 && <span className="nav-badge-dot" />}
            </button>
          );
        })}
      </nav>
      <div className="sidebar-footer">
        <button className="sidebar-user" data-tooltip={currentUser?.display_name || "User"} onClick={onAvatarClick}>
          <div className="sidebar-avatar" style={{ background: avBg, color: avColor, border: avBorder }}>{avIcon || (currentUser?.display_name?.[0]?.toUpperCase() || "U")}</div>
          <div className="sidebar-user-meta">
            <span className="sidebar-user-name">{currentUser?.display_name || "User"}</span>
            <span className="sidebar-user-email">{currentUser?.role || "member"}</span>
          </div>
          <Icons.ChevronsUpDown size={14} className="sidebar-user-chev" />
        </button>
      </div>
    </aside>
  );
}

/* ════════════ SpanTicker ════════════ */
export function SpanTicker() {
  const [rate, setRate] = useState(42);
  useEffect(() => {
    const t = setInterval(() => setRate((r) => Math.max(8, Math.min(120, r + Math.round((Math.random() - 0.5) * 12)))), 1500);
    return () => clearInterval(t);
  }, []);
  return (
    <span className="span-ticker" title="Spans per second (live)">
      <span className="pulse-dot" />
      {rate.toLocaleString()} <span style={{ color: "var(--text-muted)" }}>spans/s</span>
    </span>
  );
}

/* ════════════ Header ════════════ */
interface Crumb { label: string; href?: string; }
export function Header({ breadcrumbs, onOpenCmd, cmdOpen, mobileOpen, setMobileOpen, collapsed, setCollapsed, isMobile }: {
  breadcrumbs: Crumb[]; onOpenCmd: () => void; cmdOpen: boolean; mobileOpen: boolean; setMobileOpen: (v: boolean) => void; collapsed: boolean; setCollapsed: (v: boolean) => void; isMobile: boolean;
}) {
  const router = useRouter();
  const [notifOpen, setNotifOpen] = useState(false);
  const [notifs, setNotifs] = useState<{ id: number | string; kind: string; title: string; body: string; time: string; unread: boolean }[]>([]);
  useEffect(() => {
    fetch("/api/notifications", { credentials: "same-origin" })
      .then(r => r.ok ? r.json() : { data: [] })
      .then(d => {
        const items = d?.data || [];
        setNotifs(items.map((n: any, i: number) => ({
          id: n.id || i,
          kind: n.kind || n.type || "policy",
          title: n.title || n.message || "",
          body: n.body || n.detail || "",
          time: n.time || n.created_at || "",
          unread: n.unread ?? !n.read,
        })));
      })
      .catch(() => {});
  }, [notifOpen]);
  const unread = notifs.filter((n) => n.unread).length;
  const notifRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!notifOpen) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as HTMLElement;
      if (t.closest?.('[aria-label="Notifications"]')) return;
      if (notifRef.current && !notifRef.current.contains(t)) setNotifOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [notifOpen]);

  const kindIcon = (k: string) => {
    if (k === "block") return { Icon: Icons.AlertTriangle, color: "var(--danger)", bg: "var(--danger-bg)" };
    if (k === "approval") return { Icon: Icons.UserCheck, color: "var(--info)", bg: "var(--info-bg)" };
    if (k === "budget") return { Icon: Icons.Dollar, color: "var(--warning)", bg: "var(--warning-bg)" };
    if (k === "policy") return { Icon: Icons.Shield, color: "var(--accent)", bg: "var(--accent-bg)" };
    if (k === "compliance") return { Icon: Icons.FileCheck, color: "var(--success)", bg: "var(--success-bg)" };
    return { Icon: Icons.Hash, color: "var(--text-muted)", bg: "var(--bg-active)" };
  };

  return (
    <header className="header">
      <button className="header-mobile-trigger" onClick={() => { if (!isMobile && collapsed) setCollapsed(false); else setMobileOpen(!mobileOpen); }}
        aria-label="Open sidebar" title="Open sidebar  ⌘.">
        <Icons.PanelLeft size={18} />
      </button>
      <div className="breadcrumbs">
        {breadcrumbs.map((b, i) => {
          const isLast = i === breadcrumbs.length - 1;
          return (
            <span key={i} style={{ display: "contents" }}>
              {i > 0 && <Icons.ChevronRight size={13} className="crumb-sep" />}
              {isLast ? <span className="crumb-current">{b.label}</span>
                : <a href="#" onClick={(e) => { e.preventDefault(); if (b.href) router.push(b.href); }}>{b.label}</a>}
            </span>
          );
        })}
      </div>
      <div className="header-right">
        <button className="cmdk-button" data-open={cmdOpen || undefined} onClick={onOpenCmd}>
          <Icons.Search size={14} />
          <span>Search policies, traces, agents…</span>
          <span className="kbd">⌘K</span>
        </button>
        <button className="header-iconbtn" title="Notifications" aria-label="Notifications" onClick={() => setNotifOpen(!notifOpen)}>
          <Icons.Bell size={15} />
          {unread > 0 && <span className="notif-badge" />}
        </button>
      </div>
      {notifOpen && (
        <div className="notif-panel" ref={notifRef}>
          <div className="notif-header">
            <Icons.Bell size={14} style={{ color: "var(--text-secondary)" }} />
            <span style={{ flex: 1, fontWeight: 600, fontSize: 13.5 }}>Notifications</span>
            <button className="t-sm" style={{ color: "var(--accent)" }} onClick={() => setNotifs((ns) => ns.map((n) => ({ ...n, unread: false })))}>Mark all read</button>
          </div>
          <div className="notif-body">
            {notifs.map((n) => {
              const { Icon, color, bg } = kindIcon(n.kind);
              return (
                <div key={n.id} className={`notif-item ${n.unread ? "unread" : "read"}`} onClick={() => setNotifs((ns) => ns.map((x) => x.id === n.id ? { ...x, unread: false } : x))}>
                  <div className="notif-icon" style={{ background: bg, color }}><Icon size={14} /></div>
                  <div className="notif-text">
                    <div className="notif-title">{n.title}</div>
                    <div className="notif-body-text">{n.body}</div>
                    <div className="notif-time">{n.time}</div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </header>
  );
}

/* ════════════ UserMenu ════════════ */
export function UserMenu({ open, onClose, user }: { open: boolean; onClose: () => void; theme?: string; onToggleTheme?: () => void; setTheme?: (v: string) => void; user?: { name: string; email: string } }) {
  const router = useRouter();
  const ref = useRef<HTMLDivElement>(null);
  const [langOpen, setLangOpen] = useState(false);
  const { user: currentUser, mode } = useUser();
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as HTMLElement;
      if (ref.current && !ref.current.contains(t)) { if (t.closest?.(".sidebar-user")) return; onClose(); }
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div ref={ref} role="menu" className="user-menu-popup">
      <div className="user-menu-header"><div className="user-menu-email">{user?.email || currentUser?.email || ""}</div></div>
      <button className="user-menu-item" onClick={() => { onClose(); router.push("/settings"); }}><Icons.Settings size={14} /> Settings<Kbd>⇧⌘,</Kbd></button>
      <button className="user-menu-item" onClick={() => setLangOpen((o) => !o)} aria-expanded={langOpen}>
        <Icons.Languages size={14} /> Language
        <span style={{ marginLeft: "auto", color: "var(--text-muted)", display: "flex", alignItems: "center", gap: 6 }}>
          <span className="t-sm">English (US)</span>
          <Icons.ChevronRight size={12} style={{ transform: langOpen ? "rotate(90deg)" : "rotate(0)", transition: "transform 120ms" }} />
        </span>
      </button>
      {langOpen && (
        <div className="user-menu-submenu">
          {["English (US)", "English (UK)", "Español", "Français", "Deutsch", "日本語", "中文"].map((l, i) => (
            <button key={l} className="user-menu-item user-menu-subitem" onClick={() => setLangOpen(false)}>
              <span style={{ width: 14, display: "inline-flex", justifyContent: "center" }}>{i === 0 && <Icons.Check size={12} style={{ color: "var(--accent)" }} />}</span>
              <span>{l}</span>
            </button>
          ))}
        </div>
      )}
      <button className="user-menu-item" onClick={() => { onClose(); window.open("https://getstrathon.com/docs", "_blank", "noopener"); }}><Icons.HelpCircle size={14} /> Get help</button>
      <button className="user-menu-item" onClick={() => { onClose(); router.push("/settings?section=apikeys"); }}><Icons.Key size={14} /> API keys</button>
      <button className="user-menu-item" onClick={() => { onClose(); window.open("https://getstrathon.com/docs", "_blank", "noopener"); }}><Icons.Book size={14} /> Documentation<Icons.ExternalLink size={12} style={{ marginLeft: "auto", color: "var(--text-muted)" }} /></button>
      <button className="user-menu-item" onClick={() => { onClose(); window.open("https://github.com/strathon/strathon/issues/new", "_blank", "noopener noreferrer"); }}><Icons.AlertTriangle size={14} /> Report bug<Icons.ExternalLink size={12} style={{ marginLeft: "auto", color: "var(--text-muted)" }} /></button>
      <button className="user-menu-item" onClick={() => { onClose(); window.open("https://discord.gg/strathon", "_blank", "noopener noreferrer"); }}><Icons.Globe size={14} /> Join Discord<Icons.ExternalLink size={12} style={{ marginLeft: "auto", color: "var(--text-muted)" }} /></button>
      <div className="user-menu-divider" />
      {mode === "cloud" && (
      <button className="user-menu-upgrade user-menu-item" onClick={() => { onClose(); router.push("/settings?section=billing"); }}>
        <Icons.Sparkles size={14} /><span style={{ flex: 1 }}>Upgrade plan</span><Badge kind="accent" mono>Team</Badge>
      </button>
      )}
      <div className="user-menu-divider" />
      <button className="user-menu-item" onClick={async () => { onClose(); try { await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" }); } catch {} router.push("/login"); }} style={{ color: "var(--danger)" }}><Icons.LogOut size={14} /> Log out</button>
    </div>
  );
}

/* ════════════ CommandPalette ════════════ */
export function CommandPalette({ open, onClose, onAction, toggleTheme }: { open: boolean; onClose: () => void; onAction?: (a: string) => void; toggleTheme?: () => void }) {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const [recents, setRecents] = useState<{ label: string; icon?: string; to?: string }[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setQ(""); setActive(0);
      try { setRecents(JSON.parse(localStorage.getItem("strathon-recents") || "[]")); } catch { setRecents([]); }
      setTimeout(() => inputRef.current?.focus(), 30);
    }
  }, [open]);

  const groups = useMemo(() => {
    const items = COMMAND_ITEMS;
    const filtered = q ? items.filter((it) => it.label.toLowerCase().includes(q.toLowerCase())) : items;
    const recentItems = q ? [] : recents.slice(0, 4).map((r) => ({ section: "Recent", icon: r.icon || "Hash", label: r.label, to: r.to, kbd: undefined as string | undefined }));
    const flat = [...recentItems, ...filtered];
    const bySection: Record<string, typeof flat> = {};
    flat.forEach((it) => { (bySection[it.section] ||= []).push(it); });
    return { flat, bySection };
  }, [q, recents]);

  const pushRecent = (it: { label: string; icon?: string; to?: string }) => {
    try {
      const next = [{ label: it.label, icon: it.icon, to: it.to }, ...recents.filter((r) => r.label !== it.label)].slice(0, 5);
      localStorage.setItem("strathon-recents", JSON.stringify(next));
      setRecents(next);
    } catch {}
  };

  const handle = useCallback((it: { section?: string; to?: string; action?: string; label: string; icon?: string } | undefined) => {
    if (!it) return;
    if (it.section !== "Recent" && it.to) pushRecent(it);
    if (it.action === "toggle-theme") toggleTheme?.();
    else if (it.action === "create-policy") router.push("/policies/pol_block_prompt_injection");
    else if (it.action === "create-halt") onAction?.("create-halt");
    else if (it.action === "go-approvals") router.push("/approvals");
    else if (it.action === "go-export") router.push("/settings?section=export");
    else if (it.action === "show-shortcuts") onAction?.("show-shortcuts");
    else if (it.to) router.push(`/${it.to}`);
    onClose();
  }, [router, onAction, onClose, toggleTheme, recents]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(groups.flat.length - 1, a + 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(0, a - 1)); }
      else if (e.key === "Enter") { e.preventDefault(); handle(groups.flat[active]); }
      else if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, groups, active, handle, onClose]);

  if (!open) return null;
  let runningIdx = -1;
  return (
    <div className="cmdk-backdrop" onClick={onClose}>
      <div className="cmdk" onClick={(e) => e.stopPropagation()}>
        <div className="cmdk-input-row">
          <Icons.Search size={16} />
          <input ref={inputRef} className="cmdk-input" placeholder="Search policies, traces, agents…" value={q} onChange={(e) => { setQ(e.target.value); setActive(0); }} />
          <Kbd>esc</Kbd>
        </div>
        <div className="cmdk-list">
          {Object.entries(groups.bySection).map(([sec, items]) => (
            <div key={sec}>
              <div className="cmdk-group-label">{sec}</div>
              {items.map((it) => {
                runningIdx += 1;
                const Icon = Icons[(it.icon as keyof typeof Icons)] || Icons.Hash;
                const myIdx = runningIdx;
                return (
                  <button key={it.label} className="cmdk-item" data-active={myIdx === active} onMouseEnter={() => setActive(myIdx)} onClick={() => handle(it)}>
                    <Icon size={15} />
                    <span><Highlight text={it.label} query={q} /></span>
                    {it.kbd && <span className="meta"><Kbd>{it.kbd}</Kbd></span>}
                  </button>
                );
              })}
            </div>
          ))}
          {groups.flat.length === 0 && <div style={{ padding: 28, textAlign: "center", color: "var(--text-muted)", fontSize: 13 }}>No results for &quot;{q}&quot;.</div>}
        </div>
        <div className="cmdk-footer">
          <span><Kbd>↑↓</Kbd> navigate</span>
          <span><Kbd>↵</Kbd> select</span>
          <span><Kbd>esc</Kbd> close</span>
          <span style={{ marginLeft: "auto", opacity: 0.7 }}>Strathon command palette</span>
        </div>
      </div>
    </div>
  );
}

/* ════════════ MobileNav ════════════ */
export function MobileNav({ pendingCount }: { pendingCount: number }) {
  const route = useActiveRoute();
  const router = useRouter();
  const items: NavItem[] = [
    { id: "overview", label: "Overview", icon: "Grid" },
    { id: "policies", label: "Policies", icon: "Shield" },
    { id: "traces", label: "Traces", icon: "GitBranch" },
    { id: "approvals", label: "Approvals", icon: "UserCheck", badge: true },
  ];
  const more = [
    { id: "agents", label: "Agents", icon: "Bot" },
    { id: "spans", label: "Spans", icon: "Search" },
    { id: "audit", label: "Audit", icon: "ScrollText" },
    { id: "budgets", label: "Budgets", icon: "Dollar" },
    { id: "compliance", label: "Compliance", icon: "FileCheck" },
  ];
  const [moreOpen, setMoreOpen] = useState(false);
  const isMore = more.some((m) => m.id === route);
  return (
    <>
      <nav className="mobile-nav" role="navigation" aria-label="Bottom navigation">
        {items.map((it) => {
          const Icon = Icons[it.icon as keyof typeof Icons];
          return (
            <button key={it.id} className="mobile-nav-item" data-active={route === it.id} onClick={() => router.push(`/${it.id}`)}>
              <Icon size={18} /><span>{it.label}</span>
              {it.badge && pendingCount > 0 && <span className="badge-dot" />}
            </button>
          );
        })}
        <button className="mobile-nav-item" data-active={isMore || moreOpen} onClick={() => setMoreOpen(!moreOpen)}>
          <Icons.MoreHorizontal size={18} /><span>More</span>
        </button>
      </nav>
      {moreOpen && (
        <>
          <div style={{ position: "fixed", inset: 0, zIndex: 91 }} onClick={() => setMoreOpen(false)} />
          <div style={{ position: "fixed", bottom: 78, right: 12, zIndex: 93, background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: 14, padding: 6, boxShadow: "var(--shadow-lg)", animation: "scale-fade 140ms ease-out", transformOrigin: "bottom right", minWidth: 180 }}>
            {more.map((it) => {
              const Icon = Icons[it.icon as keyof typeof Icons];
              return <button key={it.id} className="user-menu-item" onClick={() => { router.push(`/${it.id}`); setMoreOpen(false); }}><Icon size={14} /> {it.label}</button>;
            })}
          </div>
        </>
      )}
    </>
  );
}

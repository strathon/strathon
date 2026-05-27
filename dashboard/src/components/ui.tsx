"use client";

import React, { useState, useEffect, useRef, useMemo, useCallback, createContext, useContext } from "react";
import { Icons } from "./icons";

/* ════════════ Badge ════════════ */
export function Badge({ kind = "muted", dot = false, mono = false, children }: {
  kind?: string; dot?: boolean; mono?: boolean; children: React.ReactNode;
}) {
  return (
    <span className={`badge ${kind}${mono ? " mono" : ""}`}>
      {dot && <span className="dot" />}
      {children}
    </span>
  );
}

export function ServiceDot({ idx = 0, size = 8 }: { idx?: number; size?: number }) {
  return <span className="wf-svc-dot" style={{ background: `var(--svc-${(idx % 8) + 1})`, width: size, height: size }} />;
}

export function Kbd({ children }: { children: React.ReactNode }) {
  return <span className="kbd">{children}</span>;
}

/* ════════════ CountUp ════════════ */
export function useCountUp(target: number, duration = 900) {
  const [value, setValue] = useState(0);
  useEffect(() => {
    if (prefersReducedMotion()) { setValue(target); return; }
    let raf: number;
    const start = performance.now();
    const tick = (t: number) => {
      const p = Math.min(1, (t - start) / duration);
      const eased = 1 - Math.pow(1 - p, 3);
      setValue(target * eased);
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, duration]);
  return value;
}
export function CountUp({ to, format = (n: number) => Math.round(n).toLocaleString(), duration }: {
  to: number; format?: (n: number) => string; duration?: number;
}) {
  return <>{format(useCountUp(to, duration))}</>;
}

/* ════════════ Sparkline ════════════ */
export function Sparkline({ data, width = 80, height = 20, color = "currentColor", filled = true, labels, valueFormat = (v: number) => v.toLocaleString() }: {
  data: number[]; width?: number; height?: number; color?: string; filled?: boolean; labels?: string[]; valueFormat?: (v: number) => string;
}) {
  const [hi, setHi] = useState<{ idx: number } | null>(null);
  if (!data || !data.length) return null;
  const min = Math.min(...data), max = Math.max(...data), range = max - min || 1;
  const step = data.length > 1 ? width / (data.length - 1) : width;
  const pts = data.map((v, i) => [i * step, height - ((v - min) / range) * (height - 4) - 2]);

  const smoothPath = (points: number[][]) => {
    if (points.length < 2) return "";
    let d = `M${points[0][0]},${points[0][1]}`;
    for (let i = 0; i < points.length - 1; i++) {
      const p0 = points[i - 1] || points[i], p1 = points[i], p2 = points[i + 1], p3 = points[i + 2] || p2;
      d += ` C${p1[0] + (p2[0] - p0[0]) / 6},${p1[1] + (p2[1] - p0[1]) / 6} ${p2[0] - (p3[0] - p1[0]) / 6},${p2[1] - (p3[1] - p1[1]) / 6} ${p2[0]},${p2[1]}`;
    }
    return d;
  };
  const path = smoothPath(pts);
  const area = `${path} L${width},${height} L0,${height} Z`;
  const gid = `sparkfill-${Math.round(width)}-${Math.round(height)}-${data[0]}-${data[data.length - 1]}`;

  return (
    <Tooltip content={hi ? `${labels ? labels[hi.idx] + " · " : ""}${valueFormat(data[hi.idx])}` : null} mono>
      <svg className="spark" width={width} height={height} viewBox={`0 0 ${width} ${height}`}
        onMouseMove={(e) => {
          const rect = e.currentTarget.getBoundingClientRect();
          const idx = Math.max(0, Math.min(data.length - 1, Math.round(((e.clientX - rect.left) / rect.width) * (data.length - 1))));
          setHi({ idx });
        }}
        onMouseLeave={() => setHi(null)}>
        <defs>
          <linearGradient id={gid} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.22" />
            <stop offset="100%" stopColor={color} stopOpacity="0" />
          </linearGradient>
        </defs>
        {filled && <path d={area} fill={`url(#${gid})`} />}
        <path d={path} fill="none" stroke={color} strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
        {hi && (
          <g>
            <line className="spark-cross" x1={pts[hi.idx][0]} x2={pts[hi.idx][0]} y1={0} y2={height} />
            <circle className="spark-dot" cx={pts[hi.idx][0]} cy={pts[hi.idx][1]} r={3} stroke={color} />
          </g>
        )}
      </svg>
    </Tooltip>
  );
}

/* ════════════ Ring ════════════ */
export function Ring({ value, size = 60, stroke = 5, color = "var(--accent)", label }: {
  value: number; size?: number; stroke?: number; color?: string; label?: React.ReactNode;
}) {
  const r = (size - stroke) / 2, c = 2 * Math.PI * r, pct = Math.max(0, Math.min(100, value));
  return (
    <div className="ring" style={{ width: size, height: size }}>
      <svg width={size} height={size}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" strokeWidth={stroke} className="ring-bg" />
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" strokeWidth={stroke} stroke={color}
          strokeDasharray={c} strokeDashoffset={c - (pct / 100) * c} strokeLinecap="round" />
      </svg>
      <div className="ring-label" style={{ fontSize: size > 60 ? 18 : 14, color }}>{label ?? `${Math.round(pct)}`}</div>
    </div>
  );
}

/* ════════════ Switch / Segmented / Checkbox ════════════ */
export function Switch({ on, onChange }: { on: boolean; onChange?: (v: boolean) => void }) {
  return <button role="switch" aria-checked={on} className="switch" data-on={on} onClick={() => onChange?.(!on)} />;
}
export function Segmented<T extends string>({ value, onChange, options }: {
  value: T; onChange: (v: T) => void; options: { value: T; label: React.ReactNode }[];
}) {
  return (
    <div className="seg" role="tablist">
      {options.map((o) => (
        <button key={o.value} className="seg-btn" data-active={value === o.value} onClick={() => onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  );
}
export function Checkbox({ checked, onChange }: { checked?: boolean; onChange?: (v: boolean) => void }) {
  return <span className="checkbox" data-checked={!!checked} role="checkbox" aria-checked={!!checked}
    onClick={(e) => { e.stopPropagation(); onChange?.(!checked); }} />;
}

/* ════════════ Sheet (right drawer) ════════════ */
export function Sheet({ open, onClose, title, eyebrow, wide, tabs, activeTab, onTab, footer, pinned, onTogglePin, headerExtra, children }: {
  open: boolean; onClose: () => void; title: React.ReactNode; eyebrow?: React.ReactNode; wide?: boolean;
  tabs?: { value: string; label: string }[]; activeTab?: string; onTab?: (v: string) => void;
  footer?: React.ReactNode; pinned?: boolean; onTogglePin?: () => void; headerExtra?: React.ReactNode; children: React.ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <>
      <div className="sheet-backdrop" onClick={pinned ? undefined : onClose} />
      <aside className={`sheet${wide ? " wide" : ""}`} role="dialog" aria-modal="true">
        <div className="sheet-header">
          <div style={{ flex: 1, minWidth: 0 }}>
            {eyebrow && <div className="t-caption text-muted" style={{ marginBottom: 4 }}>{eyebrow}</div>}
            <div className="sheet-title">{title}</div>
          </div>
          {headerExtra}
          {onTogglePin && (
            <button className={`sheet-pin${pinned ? " pinned" : ""}`} onClick={onTogglePin} aria-pressed={!!pinned}>
              <Icons.Pin size={14} />
            </button>
          )}
          <button className="btn icon ghost" onClick={() => { if (pinned && onTogglePin) onTogglePin(); onClose(); }} aria-label="Close">
            <Icons.X size={15} />
          </button>
        </div>
        {tabs && (
          <div className="sheet-tabs">
            {tabs.map((t) => (
              <button key={t.value} className="sheet-tab" data-active={activeTab === t.value} onClick={() => onTab?.(t.value)}>{t.label}</button>
            ))}
          </div>
        )}
        <div className="sheet-body">{children}</div>
        {footer && (
          <div style={{ borderTop: "1px solid var(--border-subtle)", padding: "12px 20px", display: "flex", gap: 8, justifyContent: "flex-end" }}>{footer}</div>
        )}
      </aside>
    </>
  );
}

/* ════════════ Toasts ════════════ */
interface Toast { id?: string; title: string; body?: string; tone?: "success" | "warning" | "danger"; duration?: number; action?: { label?: string; onClick?: () => void }; }
const ToastCtx = createContext<{ push: (t: Omit<Toast, "id">) => void; dismiss: (id: string) => void }>({ push: () => {}, dismiss: () => {} });
export function useToast() { return useContext(ToastCtx); }

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((t: Omit<Toast, "id">) => {
    const id = Math.random().toString(36).slice(2, 9);
    const duration = t.duration ?? (t.action ? 6000 : 5000);
    setItems((xs) => [...xs, { id, ...t, duration }]);
    setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), duration);
  }, []);
  const dismiss = useCallback((id: string) => setItems((xs) => xs.filter((x) => x.id !== id)), []);
  return (
    <ToastCtx.Provider value={{ push, dismiss }}>
      {children}
      <div className="toast-region" aria-live="polite">
        {items.map((t) => (
          <div className="toast" key={t.id}>
            <div style={{ width: 22, height: 22, borderRadius: 999, display: "grid", placeItems: "center", flexShrink: 0,
              background: t.tone === "danger" ? "var(--danger-bg)" : t.tone === "warning" ? "var(--warning-bg)" : "var(--success-bg)",
              color: t.tone === "danger" ? "var(--danger)" : t.tone === "warning" ? "var(--warning)" : "var(--success)" }}>
              {t.tone === "danger" ? <Icons.X size={13} /> : t.tone === "warning" ? <Icons.AlertTriangle size={13} /> : <Icons.Check size={13} />}
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 13, fontWeight: 500 }}>{t.title}</div>
              {t.body && <div className="t-sm text-secondary" style={{ marginTop: 2 }}>{t.body}</div>}
            </div>
            {t.action && (
              <button className="toast-action" onClick={() => { try { t.action?.onClick?.(); } catch {} dismiss(t.id!); }}>
                {t.action.label || "Undo"}
              </button>
            )}
            <div className="toast-progress" style={{ animation: `toast-shrink ${t.duration}ms linear forwards` }} />
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

/* ════════════ Status badges ════════════ */
export const StatusBadge = {
  ok: () => <Badge kind="success" dot>OK</Badge>,
  blocked: () => <Badge kind="danger" dot>Blocked</Badge>,
  error: () => <Badge kind="danger" dot>Error</Badge>,
  enabled: () => <Badge kind="success" dot>Enabled</Badge>,
  shadow: () => <Badge kind="warning" dot>Shadow</Badge>,
  disabled: () => <Badge kind="muted" dot>Disabled</Badge>,
} as const;

/* ════════════ CEL syntax highlighter ════════════ */
export function HighlightedCEL({ code }: { code: string }) {
  const lines = code.split("\n");
  const kw = /\b(true|false|null|in|matches|all|exists|has|size|contains)\b/;
  return (
    <div className="code" style={{ background: "transparent", border: "none", padding: 0 }}>
      {lines.map((line, i) => {
        const out: React.ReactNode[] = [];
        if (/^\s*\/\//.test(line)) {
          out.push(<span key="c" className="cm">{line}</span>);
        } else {
          let rest = line;
          while (rest.length) {
            let m: RegExpMatchArray | null;
            if ((m = rest.match(/^("(?:\\.|[^"\\])*")/))) {
              out.push(<span key={out.length} className="str">{m[1]}</span>);
            } else if ((m = rest.match(/^([a-zA-Z_]+)\(/))) {
              out.push(<span key={out.length} className="fn">{m[1]}</span>); out.push("(");
              rest = rest.slice(m[1].length + 1); continue;
            } else if ((m = rest.match(/^([a-zA-Z_][\w.]*)/))) {
              if (kw.test(m[1])) out.push(<span key={out.length} className="kw">{m[1]}</span>);
              else out.push(m[1]);
            } else if ((m = rest.match(/^(\d+(?:\.\d+)?)/))) {
              out.push(<span key={out.length} className="num">{m[1]}</span>);
            } else if ((m = rest.match(/^([+\-*/=<>!&|.,(){}\[\]?:;])/))) {
              out.push(<span key={out.length} className="op">{m[1]}</span>);
            } else { out.push(rest[0]); m = [rest[0]] as unknown as RegExpMatchArray; }
            rest = rest.slice(m && m[1] ? m[1].length : 1);
          }
        }
        return (
          <div key={i} style={{ display: "flex", gap: 14, fontFamily: "var(--font-mono)", fontSize: 12.5, lineHeight: "20px" }}>
            <span style={{ color: "var(--text-muted)", width: 24, textAlign: "right", flexShrink: 0, userSelect: "none" }}>{i + 1}</span>
            <span style={{ flex: 1, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{out}</span>
          </div>
        );
      })}
    </div>
  );
}

/* ════════════ AreaChart ════════════ */
export function AreaChart({ series, height = 200, colors, yTicks = 4 }: {
  series: number[][]; height?: number; colors: string[]; labels?: string[]; yTicks?: number;
}) {
  const N = series[0]?.length ?? 0;
  if (!N) return null;
  const W = 720, H = height, PAD = 24;
  const stack = Array.from({ length: N }, () => 0);
  const stacked = series.map((s) => s.map((v, i) => { stack[i] += v; return stack[i]; }));
  const max = Math.max(...stack);
  const x = (i: number) => PAD + (i / (N - 1)) * (W - PAD * 2);
  const y = (v: number) => H - PAD - (v / max) * (H - PAD * 2);
  const areas = stacked.map((s, layer) => {
    const baseline = layer === 0 ? Array(N).fill(0) : stacked[layer - 1];
    const top = s.map((v, i) => `${x(i)},${y(v)}`).join(" ");
    const bot = baseline.map((v: number, i: number) => `${x(i)},${y(v)}`).reverse().join(" ");
    return `M${top} L${bot} Z`;
  });
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height, display: "block" }}>
      {Array.from({ length: yTicks + 1 }, (_, i) => {
        const v = (max / yTicks) * i, yy = y(v);
        return (
          <g key={i}>
            <line x1={PAD} x2={W - PAD} y1={yy} y2={yy} stroke="var(--border-subtle)" strokeWidth="1" />
            <text x={PAD - 6} y={yy + 3} fontSize="10" textAnchor="end" fill="var(--text-muted)" fontFamily="var(--font-mono)">${Math.round(v)}</text>
          </g>
        );
      })}
      {(() => {
        const forecastStart = N - 7, total = stacked[stacked.length - 1];
        const cone: { x: number; upper: number; lower: number }[] = [];
        for (let i = forecastStart; i < N; i++) {
          const t = (i - forecastStart) / (N - 1 - forecastStart);
          const spread = total[i] * 0.18 * t;
          cone.push({ x: x(i), upper: y(total[i] + spread), lower: y(Math.max(0, total[i] - spread)) });
        }
        if (cone.length < 2) return null;
        const upper = cone.map((p, i) => `${i === 0 ? "M" : "L"}${p.x},${p.upper}`).join(" ");
        const lower = cone.map((p) => `L${p.x},${p.lower}`).reverse().join(" ");
        return <path d={`${upper} ${lower} Z`} fill="var(--accent)" opacity="0.10" />;
      })()}
      {areas.map((d, i) => <path key={i} d={d} fill={colors[i]} opacity="0.7" />)}
      {stacked.map((s, i) => <path key={"l" + i} d={"M" + s.map((v, idx) => `${x(idx)},${y(v)}`).join(" L")} fill="none" stroke={colors[i]} strokeWidth="1.4" opacity="0.9" />)}
      <line x1={x(N - 7)} x2={x(N - 7)} y1={PAD} y2={H - PAD} stroke="var(--border-emphasis)" strokeDasharray="3 3" />
      <text x={x(N - 7) + 6} y={PAD + 12} fontSize="10" fill="var(--text-muted)">forecast →</text>
    </svg>
  );
}

/* ════════════ Pagination ════════════ */
export function Pagination({ total, page, pageSize, onPage }: { total: number; page: number; pageSize: number; onPage: (p: number) => void }) {
  const pages = Math.ceil(total / pageSize) || 1;
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 10, padding: "10px 16px", borderTop: "1px solid var(--border-subtle)", fontSize: 12.5, color: "var(--text-secondary)" }}>
      <span>{(page - 1) * pageSize + 1}–{Math.min(page * pageSize, total)} of {total}</span>
      <button className="btn ghost icon sm" disabled={page === 1} onClick={() => onPage(page - 1)}><Icons.ChevronLeft size={14} /></button>
      <span style={{ fontVariantNumeric: "tabular-nums" }}>{page} / {pages}</span>
      <button className="btn ghost icon sm" disabled={page === pages} onClick={() => onPage(page + 1)}><Icons.ChevronRight size={14} /></button>
    </div>
  );
}

/* ════════════ Modal (confirm) ════════════ */
export function Modal({ open, onClose, title, body, danger, confirmLabel = "Confirm", onConfirm }: {
  open: boolean; onClose: () => void; title: React.ReactNode; body: React.ReactNode; danger?: boolean; confirmLabel?: string; onConfirm: () => void;
}) {
  if (!open) return null;
  return (
    <div className="sheet-backdrop" onClick={onClose} style={{ display: "grid", placeItems: "center" }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        width: 420, maxWidth: "92vw", background: "color-mix(in oklab, var(--bg-elevated) 90%, transparent)",
        backdropFilter: "blur(24px)", WebkitBackdropFilter: "blur(24px)", border: "1px solid var(--border)",
        borderRadius: 12, padding: 20, boxShadow: "var(--shadow-glass)", animation: "scale-fade 150ms ease-out",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8 }}>
          <div style={{ width: 32, height: 32, borderRadius: 999, display: "grid", placeItems: "center",
            background: danger ? "var(--danger-bg)" : "var(--accent-bg)", color: danger ? "var(--danger)" : "var(--accent)" }}>
            {danger ? <Icons.AlertTriangle size={16} /> : <Icons.Shield size={16} />}
          </div>
          <div style={{ fontSize: 15, fontWeight: 600 }}>{title}</div>
        </div>
        <div className="t-sm text-secondary" style={{ marginBottom: 18, marginLeft: 44 }}>{body}</div>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className={`btn ${danger ? "danger solid" : "primary"}`} onClick={() => { onConfirm(); onClose(); }}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}

/* ════════════ Dropdown ════════════ */
interface DropdownItem { divider?: boolean; icon?: React.ReactNode; label?: React.ReactNode; kbd?: string; danger?: boolean; onClick?: () => void; }
export function Dropdown({ trigger, items, align = "left", width = 200 }: {
  trigger: (s: { open: boolean; toggle: () => void }) => React.ReactNode; items: DropdownItem[]; align?: "left" | "right"; width?: number;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);
  return (
    <div ref={ref} style={{ position: "relative" }}>
      {trigger({ open, toggle: () => setOpen(!open) })}
      {open && (
        <div style={{ position: "absolute", top: "calc(100% + 4px)", [align]: 0, width, background: "var(--bg-elevated)",
          border: "1px solid var(--border)", borderRadius: 8, padding: 4, boxShadow: "var(--shadow-lg)", zIndex: 60, animation: "scale-fade 120ms ease-out" }}>
          {items.map((it, i) => it.divider
            ? <div key={i} style={{ height: 1, background: "var(--border-subtle)", margin: "4px 0" }} />
            : <button key={i} className="user-menu-item" onClick={() => { it.onClick?.(); setOpen(false); }} style={it.danger ? { color: "var(--danger)" } : undefined}>
                {it.icon}<span style={{ flex: 1 }}>{it.label}</span>{it.kbd && <Kbd>{it.kbd}</Kbd>}
              </button>
          )}
        </div>
      )}
    </div>
  );
}

/* ════════════ Empty ════════════ */
export function Empty({ icon, title, subtitle, action }: { icon: React.ReactNode; title: React.ReactNode; subtitle?: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-icon">{icon}</div>
      <div style={{ fontSize: 16, fontWeight: 600 }}>{title}</div>
      {subtitle && <div className="t-sm text-secondary" style={{ maxWidth: 380 }}>{subtitle}</div>}
      {action && <div style={{ marginTop: 8 }}>{action}</div>}
    </div>
  );
}

/* ════════════ ContextMenu ════════════ */
interface CtxItem { divider?: boolean; icon?: React.ReactNode; label?: React.ReactNode; kbd?: string; danger?: boolean; onClick?: () => void; }
export function ContextMenu({ items, children }: { items: CtxItem[]; children: React.ReactNode }) {
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  useEffect(() => {
    if (!pos) return;
    const onDoc = () => setPos(null);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setPos(null);
    document.addEventListener("click", onDoc);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("click", onDoc); document.removeEventListener("keydown", onKey); };
  }, [pos]);
  return (
    <>
      <div onContextMenu={(e) => { e.preventDefault(); setPos({ x: Math.min(e.clientX, window.innerWidth - 220), y: Math.min(e.clientY, window.innerHeight - 240) }); }} style={{ display: "contents" }}>{children}</div>
      {pos && (
        <div className="ctx-menu" style={{ left: pos.x, top: pos.y }} onClick={(e) => e.stopPropagation()}>
          {items.map((it, i) => it.divider
            ? <div key={i} style={{ height: 1, background: "var(--border-subtle)", margin: "4px 0" }} />
            : <button key={i} className={`ctx-menu-item${it.danger ? " danger" : ""}`} onClick={() => { it.onClick?.(); setPos(null); }}>
                {it.icon}<span style={{ flex: 1 }}>{it.label}</span>{it.kbd && <Kbd>{it.kbd}</Kbd>}
              </button>
          )}
        </div>
      )}
    </>
  );
}

/* ════════════ InlineEdit ════════════ */
export function InlineEdit({ value, onSave, className = "", inputStyle = {} }: { value: string; onSave?: (v: string) => void; className?: string; inputStyle?: React.CSSProperties }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => { if (editing) { inputRef.current?.focus(); inputRef.current?.select(); } }, [editing]);
  if (editing) {
    return (
      <input ref={inputRef} className={`inline-edit-input ${className}`} value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => { setEditing(false); if (draft !== value) onSave?.(draft); }}
        onKeyDown={(e) => {
          if (e.key === "Enter") { setEditing(false); if (draft !== value) onSave?.(draft); }
          if (e.key === "Escape") { setDraft(value); setEditing(false); }
        }} style={inputStyle} />
    );
  }
  return <span className={`inline-edit-trigger ${className}`} onClick={() => { setDraft(value); setEditing(true); }} title="Click to rename">{value}</span>;
}

/* ════════════ Confetti ════════════ */
export function prefersReducedMotion() {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function fireConfetti() {
  if (prefersReducedMotion()) return;
  const colors = ["#D97757", "#22d3a3", "#60a5fa", "#fbbf24", "#f472b6"];
  for (let i = 0; i < 60; i++) {
    const el = document.createElement("div");
    el.className = "confetti-piece";
    el.style.left = `${50 + (Math.random() - 0.5) * 80}%`;
    el.style.top = "30%";
    el.style.background = colors[i % colors.length];
    el.style.animationDelay = `${Math.random() * 0.3}s`;
    el.style.animationDuration = `${1.4 + Math.random()}s`;
    el.style.transform = `translateX(${(Math.random() - 0.5) * 200}px) rotate(${Math.random() * 360}deg)`;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2400);
  }
}

/* ════════════ Heatmap ════════════ */
export function Heatmap({ data, color = "var(--accent)", height = 84 }: { data: number[][]; color?: string; height?: number }) {
  const max = Math.max(...data.flat(), 1);
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2, height }}>
      {data.map((row, ri) => (
        <div key={ri} style={{ display: "grid", gridTemplateColumns: "repeat(24, 1fr)", gap: 2, flex: 1 }}>
          {row.map((v, ci) => (
            <div key={ci} className="heatmap-cell" title={`${days[ri]} ${ci}:00 · ${v}`}
              style={{ background: v === 0 ? "var(--bg-active)" : `color-mix(in oklab, ${color} ${Math.round(20 + (v / max) * 80)}%, transparent)` }} />
          ))}
        </div>
      ))}
    </div>
  );
}

/* ════════════ Highlight ════════════ */
export function Highlight({ text, query }: { text: string; query: string }) {
  if (!query) return <>{text}</>;
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return <>{text}</>;
  return <>{text.slice(0, idx)}<mark>{text.slice(idx, idx + query.length)}</mark>{text.slice(idx + query.length)}</>;
}

/* ════════════ Skeletons ════════════ */
export function Skeleton({ width, height, className = "", style }: { width?: number | string; height?: number | string; className?: string; style?: React.CSSProperties }) {
  return <span className={`skeleton ${className}`.trim()} style={{ width, height, display: "inline-block", verticalAlign: "middle", ...style }} />;
}
export function SkeletonTable({ rows = 6, columns = [40, 100, 80, 60, 80] }: { rows?: number; columns?: number[] }) {
  return (
    <div className="table-wrap" aria-busy="true">
      <div className="skel-table-row" style={{ gridTemplateColumns: columns.map(() => "1fr").join(" "), height: 42 }}>
        {columns.map((_, i) => <Skeleton key={i} className="line-sm" width="60%" />)}
      </div>
      {Array.from({ length: rows }, (_, r) => (
        <div key={r} className="skel-table-row" style={{ gridTemplateColumns: columns.map((c) => `${c}fr`).join(" "), height: 54 }}>
          {columns.map((_, c) => <Skeleton key={c} className="line" width={c === 0 ? "70%" : c === columns.length - 1 ? "40%" : "55%"} style={{ animationDelay: `${r * 0.08 + c * 0.04}s` }} />)}
        </div>
      ))}
    </div>
  );
}
export function SkeletonCards({ count = 6 }: { count?: number }) {
  return (
    <div className="agents-grid" aria-busy="true">
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="card" style={{ display: "flex", flexDirection: "column", gap: 14, animationDelay: `${i * 0.05}s` }}>
          <div style={{ display: "flex", gap: 14, alignItems: "center" }}>
            <Skeleton width={56} height={56} style={{ borderRadius: 999 }} />
            <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 8 }}>
              <Skeleton className="line-lg" width="60%" /><Skeleton className="line-sm" width="40%" />
            </div>
          </div>
          <Skeleton className="block" />
          <div style={{ display: "flex", gap: 10 }}><Skeleton className="pill" /><Skeleton className="pill" /><Skeleton className="pill" /></div>
        </div>
      ))}
    </div>
  );
}

/* ════════════ MobileSheet ════════════ */
export function MobileSheet({ open, onClose, title, children, footer }: { open: boolean; onClose: () => void; title: React.ReactNode; children: React.ReactNode; footer?: React.ReactNode }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { window.removeEventListener("keydown", onKey); document.body.style.overflow = prev; };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <>
      <div className="mobile-sheet-backdrop" onClick={onClose} />
      <aside className="mobile-sheet" role="dialog" aria-modal="true">
        <div className="mobile-sheet-handle" onClick={onClose} />
        <div className="mobile-sheet-header">
          <span style={{ flex: 1, fontWeight: 600, fontSize: 15 }}>{title}</span>
          <button className="btn icon ghost" onClick={onClose} aria-label="Close"><Icons.X size={16} /></button>
        </div>
        <div className="mobile-sheet-body">{children}</div>
        {footer && <div className="mobile-sheet-footer">{footer}</div>}
      </aside>
    </>
  );
}

/* ════════════ Tooltip ════════════ */
export function Tooltip({ content, children, mono, rich, side = "top", delay = 200, disabled }: {
  content: React.ReactNode; children: React.ReactNode; mono?: boolean; rich?: boolean; side?: "top" | "bottom"; delay?: number; disabled?: boolean;
}) {
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  const ref = useRef<HTMLSpanElement>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const open = (e: React.MouseEvent | React.FocusEvent) => {
    if (disabled || content == null || content === "") return;
    if (timerRef.current) clearTimeout(timerRef.current);
    const target = e.currentTarget as HTMLElement;
    timerRef.current = setTimeout(() => {
      const r = target?.getBoundingClientRect?.() || ref.current?.getBoundingClientRect();
      if (!r) return;
      setPos({ x: r.left + r.width / 2, y: side === "bottom" ? r.bottom + 8 : r.top - 8 });
    }, delay);
  };
  const close = () => { if (timerRef.current) clearTimeout(timerRef.current); setPos(null); };
  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current); }, []);
  return (
    <>
      <span ref={ref} className="tt-wrap" onMouseEnter={open} onMouseLeave={close} onFocus={open} onBlur={close}>{children}</span>
      {pos && content != null && (
        <div className={`tt-bubble${mono ? " mono" : ""}${rich ? " rich" : ""}`} role="tooltip"
          style={{ left: Math.max(8, Math.min(typeof window !== "undefined" ? window.innerWidth - 8 : pos.x, pos.x)), top: pos.y,
            transform: side === "bottom" ? "translate(-50%, 0)" : "translate(-50%, -100%)" }}>{content}</div>
      )}
    </>
  );
}

/* ════════════ Truncated ════════════ */
export function Truncated({ text, maxWidth, mono, className = "", style = {} }: { text: string; maxWidth?: number | string; mono?: boolean; className?: string; style?: React.CSSProperties }) {
  return (
    <Tooltip content={text} mono={mono}>
      <span className={className} style={{ display: "inline-block", maxWidth: maxWidth || "100%", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", verticalAlign: "bottom", ...style }}>{text}</span>
    </Tooltip>
  );
}

/* ════════════ Time ════════════ */
const TIME_BASIS = Date.now();
export function parseAgo(ago?: string): Date {
  if (!ago) return new Date(TIME_BASIS);
  const s = String(ago).trim().toLowerCase();
  if (s === "now" || s.startsWith("just")) return new Date(TIME_BASIS - 4000);
  if (s === "yesterday") return new Date(TIME_BASIS - 86400000);
  const m = s.match(/(\d+)\s*(s|sec|seconds?|m|min|minutes?|h|hr|hours?|d|days?|w|weeks?|mo|months?|y|years?)\b/);
  if (!m) return new Date(TIME_BASIS);
  const n = parseInt(m[1], 10), unit = m[2];
  const ms = unit.startsWith("s") ? n * 1000 : unit.startsWith("mi") || unit === "m" ? n * 60000 :
    unit.startsWith("h") ? n * 3600000 : unit.startsWith("d") ? n * 86400000 :
    unit.startsWith("w") ? n * 604800000 : unit.startsWith("mo") ? n * 2592000000 :
    unit.startsWith("y") ? n * 31536000000 : 0;
  return new Date(TIME_BASIS - ms);
}
export function fmtExactTime(d: Date): string {
  if (!d) return "";
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  return `${d.toLocaleString(undefined, { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })} (${tz})`;
}
export function Time({ ago, className = "" }: { ago?: string; className?: string }) {
  const d = useMemo(() => parseAgo(ago), [ago]);
  return <Tooltip content={fmtExactTime(d)}><span className={className}>{ago}</span></Tooltip>;
}

/* ════════════ CopyableCode ════════════ */
export function CopyableCode({ children, language = "", filename, defaultWrap = false, style }: {
  children: React.ReactNode; language?: string; filename?: string; mono?: boolean; defaultWrap?: boolean; style?: React.CSSProperties;
}) {
  const [wrap, setWrap] = useState(defaultWrap);
  const [copied, setCopied] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const copy = useCallback(() => {
    const text = ref.current?.innerText || "";
    navigator.clipboard?.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1300); });
  }, []);
  return (
    <div className={`code-wrap${wrap ? " wrap" : ""}`}>
      <div className="code-wrap-head">
        <span>{filename || language || "snippet"}</span>
        <span className="grow" />
        <button className={`icon-btn${wrap ? " active" : ""}`} onClick={() => setWrap((w) => !w)} title="Toggle line wrap"><Icons.AlignLeft size={11} /> wrap</button>
        <button className={`icon-btn${copied ? " code-copy-flash" : ""}`} onClick={copy} title="Copy">{copied ? <><Icons.Check size={11} /> copied</> : <><Icons.Copy size={11} /> copy</>}</button>
      </div>
      <div ref={ref} className="code" style={{ background: "transparent", border: "none", borderRadius: 0, ...style }}>{children}</div>
    </div>
  );
}

/* ════════════ ShortcutHelp ════════════ */
export function ShortcutHelp({ open, onClose }: { open: boolean; onClose: () => void }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  const Group = ({ title, items }: { title: React.ReactNode; items: [string[], string][] }) => (
    <div>
      <div className="shortcut-group-title">{title}</div>
      {items.map(([keys, label]) => (
        <div className="shortcut-row" key={label}>
          <span>{label}</span>
          <span className="shortcut-keys">{keys.map((k, i) => <Kbd key={i}>{k}</Kbd>)}</span>
        </div>
      ))}
    </div>
  );
  return (
    <div className="sheet-backdrop" onClick={onClose} style={{ display: "grid", placeItems: "center" }}>
      <div onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-label="Keyboard shortcuts"
        style={{ width: 720, maxWidth: "92vw", background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: 14, padding: "20px 24px 16px", boxShadow: "var(--shadow-lg)", animation: "scale-fade 160ms ease-out" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 18 }}>
          <Icons.Command size={16} style={{ color: "var(--accent)" }} />
          <div style={{ fontSize: 16, fontWeight: 600, flex: 1 }}>Keyboard shortcuts</div>
          <button className="btn icon ghost" onClick={onClose} aria-label="Close"><Icons.X size={15} /></button>
        </div>
        <div className="shortcuts-grid">
          <Group title={<><Icons.Search size={11} /> Global</>} items={[[["⌘", "K"], "Open command palette"], [["/"], "Focus search on this page"], [["?"], "Show this overlay"], [["Esc"], "Close any modal/sheet/palette"]]} />
          <Group title={<><Icons.PanelLeft size={11} /> Layout</>} items={[[["⌘", "."], "Toggle sidebar"], [["["], "Toggle sidebar (no modifier)"]]} />
          <Group title={<><Icons.GitBranch size={11} /> Navigate</>} items={[[["G", "O"], "Go to Overview"], [["G", "P"], "Go to Policies"], [["G", "T"], "Go to Traces"], [["G", "S"], "Go to Spans"], [["G", "A"], "Go to Approvals"], [["G", "N"], "Go to Agents"], [["G", "U"], "Go to Audit"], [["G", "B"], "Go to Budgets"], [["G", "C"], "Go to Compliance"]]} />
          <Group title={<><Icons.Zap size={11} /> Detail pages</>} items={[[["⌘", "S"], "Save current policy"], [["P"], "Pin selected span in waterfall"], [["↑", "↓"], "Navigate list / palette"], [["↵"], "Open / activate selection"]]} />
        </div>
      </div>
    </div>
  );
}

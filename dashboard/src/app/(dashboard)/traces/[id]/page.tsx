"use client";

import { useState, useRef, useEffect, useMemo } from "react";
import { useRouter, useParams } from "next/navigation";
import { Icons } from "@/components/icons";
import { Badge, StatusBadge, Segmented, Sheet, ServiceDot, CopyableCode, Skeleton } from "@/components/ui";
import { useApi } from "@/lib/api-client";

interface WaterfallSpan {
  id: string; parent: string | null; depth: number; name: string;
  service: number; start: number; dur: number; status: "ok" | "blocked" | "error"; blockedBy?: string;
  service_name?: string;
}

const SERVICES = ["orchestrator", "tool-shell", "tool-http", "vector-store", "llm-gateway", "policy-engine", "memory-store", "tool-sql"];

function FlameView({ spans, totalDur, onSelect, selected }: { spans: WaterfallSpan[]; totalDur: number; onSelect: (id: string) => void; selected: string | null }) {
  const byDepth: Record<number, WaterfallSpan[]> = {};
  spans.forEach((s) => { (byDepth[s.depth] ||= []).push(s); });
  const depths = Object.keys(byDepth).map(Number).sort((a, b) => a - b);
  const ROW_H = 28;
  return (
    <div style={{ position: "relative" }}>
      <div className="t-sm text-secondary" style={{ marginBottom: 8 }}>Flame view — bar width is total duration; stacked by call depth.</div>
      <div style={{ position: "relative", height: depths.length * ROW_H + 8, background: "var(--bg-surface)", borderRadius: 8, padding: 4 }}>
        {depths.map((d) => (
          <div key={d} style={{ position: "absolute", left: 4, right: 4, top: d * ROW_H + 4, height: ROW_H - 2 }}>
            {byDepth[d].map((s) => {
              const left = (s.start / totalDur) * 100;
              const w = Math.max(0.3, (s.dur / totalDur) * 100);
              const color = s.status === "blocked" ? "var(--danger)" : `var(--svc-${(s.service % 8) + 1})`;
              return (
                <div key={s.id} onClick={() => onSelect(s.id)} title={`${s.name} · ${s.dur}ms`}
                  style={{ position: "absolute", left: `${left}%`, width: `${w}%`, top: 2, bottom: 2, background: color, borderRadius: 3,
                    border: selected === s.id ? "1.5px solid var(--text)" : `1px solid color-mix(in oklab, ${color} 60%, black)`,
                    display: "flex", alignItems: "center", padding: "0 6px", fontSize: 10.5, fontFamily: "var(--font-mono)", color: "rgba(255,255,255,0.95)", overflow: "hidden", whiteSpace: "nowrap", textOverflow: "ellipsis", cursor: "pointer", transition: "transform 100ms" }}
                  onMouseEnter={(e) => (e.currentTarget.style.transform = "translateY(-1px)")}
                  onMouseLeave={(e) => (e.currentTarget.style.transform = "translateY(0)")}>
                  {w > 5 ? `${s.name.replace("tool.call ", "")} · ${s.dur}ms` : ""}
                </div>
              );
            })}
          </div>
        ))}
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", marginTop: 8, fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
        <span>0ms</span><span>{Math.round(totalDur / 4)}ms</span><span>{Math.round(totalDur / 2)}ms</span><span>{Math.round((totalDur * 3) / 4)}ms</span><span>{totalDur}ms</span>
      </div>
    </div>
  );
}

function DependencyGraph({ spans, onSelect }: { spans: WaterfallSpan[]; onSelect: (id: string) => void; selected: string | null }) {
  const edges = new Map<string, number>();
  const services = new Set<number>();
  spans.forEach((s) => services.add(s.service));
  spans.forEach((s) => {
    if (!s.parent) return;
    const parent = spans.find((p) => p.id === s.parent);
    if (!parent || parent.service === s.service) return;
    const key = `${parent.service}-${s.service}`;
    edges.set(key, (edges.get(key) || 0) + 1);
  });
  const svcList = [...services].sort((a, b) => a - b);
  const W = 720, H = 380, cx = W / 2, cy = H / 2, r = 140;
  const nodes = svcList.map((svc, i) => {
    const angle = (i / svcList.length) * Math.PI * 2 - Math.PI / 2;
    return { svc, x: cx + Math.cos(angle) * r, y: cy + Math.sin(angle) * r };
  });
  const nodeBy = Object.fromEntries(nodes.map((n) => [n.svc, n]));
  const counts: Record<number, number> = {};
  spans.forEach((s) => { counts[s.service] = (counts[s.service] || 0) + 1; });
  const maxCount = Math.max(...Object.values(counts), 1);

  return (
    <div>
      <div className="t-sm text-secondary" style={{ marginBottom: 8 }}>Service-to-service call graph. Edge thickness = call count.</div>
      <div style={{ background: "var(--bg-surface)", borderRadius: 8, padding: 12 }}>
        <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: H, display: "block" }}>
          <defs>
            <marker id="arrowhead" viewBox="0 -5 10 10" refX="8" refY="0" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,-5 L10,0 L0,5" fill="var(--border-emphasis)" /></marker>
          </defs>
          {[...edges.entries()].map(([key, count]) => {
            const [a, b] = key.split("-").map(Number);
            const na = nodeBy[a], nb = nodeBy[b];
            if (!na || !nb) return null;
            const dx = nb.x - na.x, dy = nb.y - na.y;
            const len = Math.sqrt(dx * dx + dy * dy) || 1;
            const ux = dx / len, uy = dy / len;
            const x1 = na.x + ux * 22, y1 = na.y + uy * 22, x2 = nb.x - ux * 30, y2 = nb.y - uy * 30;
            const sw = Math.min(4, 0.8 + count * 0.5);
            return (
              <g key={key}>
                <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--border-emphasis)" strokeWidth={sw} markerEnd="url(#arrowhead)" opacity="0.6" />
                <text x={(x1 + x2) / 2} y={(y1 + y2) / 2 - 4} fontSize="10" fill="var(--text-muted)" fontFamily="var(--font-mono)" textAnchor="middle">{count}</text>
              </g>
            );
          })}
          {nodes.map((n) => {
            const c = counts[n.svc] || 0;
            const size = 18 + (c / maxCount) * 12;
            const color = `var(--svc-${(n.svc % 8) + 1})`;
            return (
              <g key={n.svc} style={{ cursor: "pointer" }} onClick={() => { const sp = spans.find((s) => s.service === n.svc); if (sp) onSelect(sp.id); }}>
                <circle cx={n.x} cy={n.y} r={size + 4} fill="none" stroke={color} strokeWidth="1" opacity="0.3" />
                <circle cx={n.x} cy={n.y} r={size} fill={color} opacity="0.85" />
                <text x={n.x} y={n.y - size - 8} fontSize="11" fontWeight="600" fill="var(--text)" textAnchor="middle" fontFamily="var(--font-mono)">{SERVICES[n.svc]}</text>
                <text x={n.x} y={n.y + 4} fontSize="11" fontWeight="600" fill="white" textAnchor="middle" fontFamily="var(--font-mono)">{c}</text>
              </g>
            );
          })}
        </svg>
      </div>
    </div>
  );
}

export default function WaterfallPage() {
  const router = useRouter();
  const params = useParams();
  const id = Array.isArray(params.id) ? params.id[0] : params.id;
  
  const { data: traceData, loading: traceLoading, error: traceError, refetch } = useApi<{ data: any }>(`/api/traces/${id}`);
  const traceResp = traceData?.data || traceData;
  const trace = traceResp || { id, agent: "", operation: "", status: "ok", started: "", model: "", durationMs: 0, spans: 0 };
  const spans: WaterfallSpan[] = traceResp?.spans || traceResp?.waterfall_spans || [];
  const totalDur = useMemo(() => spans.length > 0 ? Math.max(...spans.map((s) => s.start + s.dur)) : 1, [spans]);

  if (traceLoading) return <div className="page"><Skeleton width="100%" height={400} /></div>;
  if (traceError) return <div className="page"><div className="card" style={{ padding: 24, textAlign: "center" }}><div style={{ color: "var(--danger)", marginBottom: 8 }}>{traceError}</div><button className="btn" onClick={refetch}>Retry</button></div></div>;

  const [selected, setSelected] = useState<string | null>(null);
  const [pinned, setPinned] = useState(false);
  const [hovered, setHovered] = useState<WaterfallSpan | null>(null);
  const [tipPos, setTipPos] = useState({ x: 0, y: 0 });
  const [viewMode, setViewMode] = useState("timeline");
  const [showCritical, setShowCritical] = useState(false);
  const [sheetTab, setSheetTab] = useState("attrs");
  const [tab, setTab] = useState("waterfall");
  const [search, setSearch] = useState("");
  const [brush, setBrush] = useState({ start: 0, end: 1 });
  const minimapRef = useRef<HTMLDivElement>(null);

  const criticalIds = useMemo(() => {
    const childrenOf = (pid: string) => spans.filter((s) => s.parent === pid);
    const root = spans.find((s) => s.parent === null);
    if (!root) return new Set<string>();
    const path = new Set<string>();
    let cur: WaterfallSpan | undefined = root;
    while (cur) {
      path.add(cur.id);
      const kids = childrenOf(cur.id);
      if (!kids.length) break;
      cur = kids.reduce((best, k) => (k.start + k.dur > best.start + best.dur ? k : best), kids[0]);
    }
    return path;
  }, [spans]);

  const view = useMemo(() => {
    const visibleStart = brush.start * totalDur, visibleEnd = brush.end * totalDur;
    return { visibleStart, visibleEnd, visibleDur: visibleEnd - visibleStart };
  }, [brush, totalDur]);

  const matches = useMemo(() => {
    if (!search) return new Set<string>();
    return new Set(spans.filter((s) => s.name.toLowerCase().includes(search.toLowerCase())).map((s) => s.id));
  }, [search, spans]);

  const dragRef = useRef<{ startX: number; startBrush: { start: number; end: number } } | null>(null);
  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragRef.current || !minimapRef.current) return;
      const rect = minimapRef.current.getBoundingClientRect();
      const dx = (e.clientX - dragRef.current.startX) / rect.width;
      let s = dragRef.current.startBrush.start + dx, en = dragRef.current.startBrush.end + dx;
      const w = en - s;
      if (s < 0) { s = 0; en = w; }
      if (en > 1) { en = 1; s = 1 - w; }
      setBrush({ start: s, end: en });
    };
    const onUp = () => { dragRef.current = null; document.body.classList.remove("is-resizing"); };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => { window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); };
  }, []);

  const ticks = useMemo(() => {
    const n = 8, step = view.visibleDur / n;
    return Array.from({ length: n + 1 }, (_, i) => view.visibleStart + i * step);
  }, [view]);

  const selectedSpan = spans.find((s) => s.id === selected);

  const [treeW, setTreeW] = useState(360);
  const treeDragRef = useRef<{ left: number } | null>(null);
  useEffect(() => {
    const onMove = (e: MouseEvent) => { if (!treeDragRef.current) return; setTreeW(Math.max(240, Math.min(540, e.clientX - treeDragRef.current.left))); };
    const onUp = () => { treeDragRef.current = null; document.body.style.cursor = ""; };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => { window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); };
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const inField = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName ?? "");
      if (inField || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key.toLowerCase() === "p" && selected) { e.preventDefault(); setPinned((p) => !p); }
      if (e.key === "Escape" && pinned) setPinned(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected, pinned]);

  return (
    <div className="page full">
      <div className="waterfall-shell">
        <div className="waterfall-toolbar">
          <div>
            <div className="t-caption text-muted">Trace</div>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 4 }}>
              <span className="mono" style={{ fontSize: 14, fontWeight: 500 }}>{trace.id}</span>
              <button className="btn ghost icon sm" title="Copy" onClick={() => navigator.clipboard?.writeText(trace.id)}><Icons.Copy size={13} /></button>
              {StatusBadge[trace.status as keyof typeof StatusBadge]?.() || trace.status}
            </div>
            <div className="t-sm text-secondary" style={{ marginTop: 4 }}>
              <span className="mono">{trace.operation}</span> · {trace.agent} · {trace.spans} spans · {(totalDur / 1000).toFixed(2)} s · {trace.started}
            </div>
          </div>
          <div style={{ flex: 1 }} />
          <div className="input-wrap" style={{ width: 260 }}>
            <Icons.Search size={14} />
            <input className="input search" placeholder="Highlight spans by name…" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          <Segmented value={viewMode} onChange={setViewMode} options={[{ label: "Timeline", value: "timeline" }, { label: "Flame", value: "flame" }, { label: "Graph", value: "graph" }]} />
          <button className="btn ghost sm" data-active={showCritical} onClick={() => setShowCritical((s) => !s)}
            style={showCritical ? { background: "var(--accent-bg)", color: "var(--accent)", border: "1px solid var(--accent-border)" } : undefined} title="Highlight the longest dependency chain">
            <Icons.Zap size={12} /> Critical path
          </button>
          <div className="tabs" style={{ margin: 0, border: 0 }}>
            <button className="tab" data-active={tab === "waterfall"} onClick={() => setTab("waterfall")}>Waterfall</button>
            <button className="tab" data-active={tab === "spans"} onClick={() => setTab("spans")}>Spans table</button>
            <button className="tab" data-active={tab === "logs"} onClick={() => setTab("logs")}>Logs</button>
          </div>
        </div>

        <div className="minimap">
          <div className="minimap-bg" ref={minimapRef}>
            {spans.map((s) => {
              const left = (s.start / totalDur) * 100, w = Math.max(0.4, (s.dur / totalDur) * 100);
              const color = s.status === "blocked" ? "var(--danger)" : `var(--svc-${(s.service % 8) + 1})`;
              return <div key={s.id} style={{ position: "absolute", left: `${left}%`, width: `${w}%`, top: 6 + s.depth * 8, height: 4, background: color, opacity: 0.85, borderRadius: 1 }} />;
            })}
            <div className="minimap-brush" style={{ left: `${brush.start * 100}%`, width: `${(brush.end - brush.start) * 100}%` }}
              onMouseDown={(e) => { dragRef.current = { startX: e.clientX, startBrush: brush }; document.body.classList.add("is-resizing"); }} />
          </div>
        </div>

        {tab === "waterfall" && viewMode === "timeline" && (
          <div className="waterfall-body" style={{ gridTemplateColumns: `${treeW}px 5px 1fr` }}>
            <div className="waterfall-tree">
              <div className="wf-time-ruler" style={{ display: "flex", alignItems: "center", padding: "0 12px", color: "var(--text-muted)", fontWeight: 500, fontFamily: "var(--font-sans)", fontSize: 11, letterSpacing: "0.06em", textTransform: "uppercase" }}>Name · service · duration</div>
              {spans.map((s) => {
                const dim = matches.size && !matches.has(s.id);
                return (
                  <div key={s.id} className="wf-row" data-selected={selected === s.id} style={{ paddingLeft: 12 + s.depth * 14, opacity: dim ? 0.35 : 1 }} onClick={() => setSelected(s.id)} title={s.name}>
                    <Icons.ChevronDown size={11} className="wf-caret" />
                    <ServiceDot idx={s.service} />
                    <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.name}</span>
                    <span style={{ color: "var(--text-muted)", fontVariantNumeric: "tabular-nums", fontFamily: "var(--font-mono)", fontSize: 11 }}>{s.dur}ms</span>
                  </div>
                );
              })}
            </div>
            <div onMouseDown={(e) => { treeDragRef.current = { left: e.currentTarget.getBoundingClientRect().left - treeW }; document.body.style.cursor = "ew-resize"; }} style={{ cursor: "ew-resize", background: "var(--border-subtle)" }} title="Resize" />
            <div className="waterfall-timeline">
              <div className="wf-time-ruler">
                {ticks.map((t, i) => <div key={i} className="wf-tick" style={{ left: `${((t - view.visibleStart) / view.visibleDur) * 100}%` }}>{(t / 1000).toFixed(2)}s</div>)}
              </div>
              {spans.map((s) => {
                const startRel = (s.start - view.visibleStart) / view.visibleDur;
                const left = Math.max(0, startRel * 100);
                const w = Math.max(0.1, (s.dur / view.visibleDur) * 100);
                const dim = matches.size && !matches.has(s.id);
                const color = s.status === "blocked" ? "var(--danger)" : `var(--svc-${(s.service % 8) + 1})`;
                const crit = showCritical && criticalIds.has(s.id);
                return (
                  <div key={s.id} className="wf-bar-row" data-selected={selected === s.id} data-crit={crit} onClick={() => setSelected(s.id)}
                    onDoubleClick={() => { const pad = totalDur * 0.05; setBrush({ start: Math.max(0, (s.start - pad) / totalDur), end: Math.min(1, (s.start + s.dur + pad) / totalDur) }); }}
                    onMouseEnter={(e) => { setHovered(s); setTipPos({ x: e.clientX, y: e.currentTarget.getBoundingClientRect().top }); }}
                    onMouseMove={(e) => setTipPos({ x: e.clientX, y: e.currentTarget.getBoundingClientRect().top })}
                    onMouseLeave={() => setHovered(null)} style={{ opacity: dim ? 0.35 : 1 }}>
                    <div className={`wf-bar${s.status === "blocked" ? " blocked" : ""}${crit ? " crit" : ""}`} style={{ left: `${left}%`, width: `${w}%`, background: color, boxShadow: selected === s.id ? "0 0 0 1.5px var(--text)" : "none" }}>
                      {w > 4 ? `${s.dur}ms` : ""}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {tab === "waterfall" && viewMode === "flame" && <div style={{ padding: "16px 24px", overflow: "auto" }}><FlameView spans={spans} totalDur={totalDur} onSelect={setSelected} selected={selected} /></div>}
        {tab === "waterfall" && viewMode === "graph" && <div style={{ padding: "16px 24px", overflow: "auto" }}><DependencyGraph spans={spans} onSelect={setSelected} selected={selected} /></div>}

        {tab === "spans" && (
          <div style={{ padding: "16px 24px", overflow: "auto" }}>
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Span</th><th>Service</th><th style={{ textAlign: "right" }}>Start</th><th style={{ textAlign: "right" }}>Duration</th><th>Status</th></tr></thead>
                <tbody>
                  {spans.map((s) => (
                    <tr key={s.id} className="clickable" onClick={() => setSelected(s.id)}>
                      <td className="mono" style={{ fontSize: 12.5 }}>{s.name}</td>
                      <td><div style={{ display: "flex", alignItems: "center", gap: 8 }}><ServiceDot idx={s.service} />{SERVICES[s.service]}</div></td>
                      <td className="mono" style={{ textAlign: "right", fontSize: 12 }}>{s.start}ms</td>
                      <td className="mono" style={{ textAlign: "right", fontSize: 12 }}>{s.dur}ms</td>
                      <td>{StatusBadge[s.status]()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {tab === "logs" && (
          <div style={{ padding: "16px 24px", overflow: "auto" }}>
            <CopyableCode filename={`trace-${trace.shortId}.log`}>
{`[14:32:01.024] agent.run started agent=atlas-support trace=${trace.shortId}
[14:32:01.032] agent.plan model=claude-sonnet-4 tokens.in=412
[14:32:01.327] policy.evaluate count=14 matched=[require-approval-on-writes]
[14:32:01.348] tool.call vector.search kb=kb-docs k=8 ms=142
[14:32:01.500] llm.complete tokens.in=1842 tokens.out=287 ms=612
[14:32:02.460] policy.evaluate count=14 matched=[block-secret-leakage]
[14:32:02.478] BLOCK tool.call=http.request reason="output contained bearer token"
[14:32:02.488] agent.replan
[14:32:02.660] tool.call email.send to="customer-…" ms=311
[14:32:03.020] memory.write key=run_summary ttl=86400
[14:32:03.434] agent.run completed status=ok`}
            </CopyableCode>
          </div>
        )}
      </div>

      {hovered && (
        <div style={{ position: "fixed", left: tipPos.x + 14, top: tipPos.y - 8, transform: "translateY(-100%)", background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: 8, padding: "8px 10px", boxShadow: "var(--shadow-lg)", zIndex: 60, pointerEvents: "none", fontSize: 12, minWidth: 180 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
            <ServiceDot idx={hovered.service} />
            <span className="mono" style={{ fontWeight: 600 }}>{hovered.name}</span>
          </div>
          <div className="t-sm text-secondary" style={{ display: "flex", gap: 10, fontVariantNumeric: "tabular-nums" }}>
            <span>{SERVICES[hovered.service]}</span><span>·</span><span style={{ color: "var(--text)" }}>{hovered.dur}ms</span><span>·</span><span>start +{hovered.start}ms</span>
          </div>
          {hovered.status === "blocked" && <div style={{ marginTop: 6, fontSize: 11.5, color: "var(--danger)", display: "flex", alignItems: "center", gap: 4 }}><Icons.AlertTriangle size={11} /> Blocked by {hovered.blockedBy}</div>}
        </div>
      )}

      <Sheet open={!!selected} onClose={() => { setSelected(null); setPinned(false); }} pinned={pinned} onTogglePin={() => setPinned((p) => !p)}
        eyebrow="Span" title={selectedSpan?.name}
        tabs={[{ value: "attrs", label: "Attributes" }, { value: "policy", label: "Policy hits" }, { value: "payload", label: "Tool payload" }]}
        activeTab={sheetTab} onTab={setSheetTab}>
        {selectedSpan && sheetTab === "attrs" && (
          <div>
            <div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}>{StatusBadge[selectedSpan.status]()}<Badge mono>{SERVICES[selectedSpan.service]}</Badge><Badge>{selectedSpan.dur}ms</Badge></div>
            <div className="t-caption text-muted" style={{ marginBottom: 8 }}>Attributes</div>
            <CopyableCode filename={`${selectedSpan.id}.attrs`} defaultWrap>
{`span.id        = "${selectedSpan.id}"
span.name      = "${selectedSpan.name}"
span.parent    = ${selectedSpan.parent ? `"${selectedSpan.parent}"` : "null"}
service.name   = "${SERVICES[selectedSpan.service]}"
agent.id       = "${trace.agent}"
duration_ms    = ${selectedSpan.dur}
start_offset   = ${selectedSpan.start}ms
status         = "${selectedSpan.status}"
${selectedSpan.blockedBy ? `policy.blocked_by = "${selectedSpan.blockedBy}"` : ""}`}
            </CopyableCode>
          </div>
        )}
        {selectedSpan && sheetTab === "policy" && (
          <div>
            {selectedSpan.blockedBy ? (
              <div className="card dense" style={{ background: "var(--danger-bg)", borderColor: "var(--danger-border)" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}><Icons.AlertTriangle size={14} style={{ color: "var(--danger)" }} /><span style={{ fontWeight: 600 }}>Blocked by {selectedSpan.blockedBy}</span></div>
                <div className="t-sm text-secondary">Output matched secret-leakage pattern. The tool call was prevented; no external request was made.</div>
                <div style={{ marginTop: 12 }}><button className="btn sm" onClick={() => router.push("/policies/pol_secret_leakage")}><Icons.Shield size={12} /> Open policy</button></div>
              </div>
            ) : <div className="t-sm text-secondary">All policies passed for this span.</div>}
          </div>
        )}
        {selectedSpan && sheetTab === "payload" && (
          <div className="code">
{`{
  "tool": "${selectedSpan.name.replace("tool.call ", "")}",
  "input": {
    "endpoint": "https://api.example.com/v1/customers",
    "method": "GET",
    "headers": { "authorization": "Bearer ******" }
  },
  ${selectedSpan.status === "blocked" ? `"blocked": true,
  "reason": "${selectedSpan.blockedBy}"` : `"output_preview": "(287 tokens, truncated)"`}
}`}
          </div>
        )}
      </Sheet>
    </div>
  );
}

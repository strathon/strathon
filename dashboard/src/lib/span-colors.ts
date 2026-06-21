/**
 * Span-kind color palette. Shared by the trace detail waterfall and the
 * spans list so a kind dot in one matches the bar color in the other. Uses
 * the dashboard's semantic CSS variables so colors adapt across light and
 * dark themes. Blocked overrides kind and is handled by spanColor().
 */
export const KIND_COLOR: Record<string, string> = {
  agent: "var(--kind-agent)",
  llm: "var(--kind-llm)",
  tool: "var(--kind-tool)",
  retrieval: "var(--kind-retrieval)",
  other: "var(--kind-other)",
};

export type SpanKind = keyof typeof KIND_COLOR;

interface SpanLike {
  kind?: SpanKind;
  status?: "ok" | "blocked" | "error";
}

/** Resolve the color for a span: blocked overrides; otherwise look up kind. */
export function spanColor(s: SpanLike): string {
  if (s.status === "blocked") return "var(--danger)";
  return KIND_COLOR[s.kind ?? "other"] ?? "var(--info)";
}

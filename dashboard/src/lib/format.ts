// Date/time formatting helpers. The receiver stores and returns timestamps as
// UTC ISO strings; these render them in the viewer's local timezone in a
// human-readable form. The browser supplies the timezone automatically via
// Intl, so no manual timezone selection is required.

function parse(value: string | number | Date | null | undefined): Date | null {
  if (!value) return null;
  const d = value instanceof Date ? value : new Date(value);
  return isNaN(d.getTime()) ? null : d;
}

/** e.g. "Jun 2, 2026, 3:25 PM" in the user's local timezone. */
export function formatDateTime(value: string | number | Date | null | undefined): string {
  const d = parse(value);
  if (!d) return "—";
  return d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit",
    // Label the zone (PST / IST shows as GMT+5:30 / UTC) so an absolute time
    // is never ambiguous about which timezone it is in.
    timeZoneName: "short",
  });
}

/** e.g. "Jun 2, 2026" (date only), local timezone. */
export function formatDate(value: string | number | Date | null | undefined): string {
  const d = parse(value);
  if (!d) return "—";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/** Relative time for recent events, e.g. "just now", "5 min ago", "2 days ago". */
export function formatRelative(value: string | number | Date | null | undefined): string {
  const d = parse(value);
  if (!d) return "—";
  const diffMs = Date.now() - d.getTime();
  const sec = Math.round(diffMs / 1000);
  if (sec < 60) return "just now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} min ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr} hr ago`;
  const day = Math.round(hr / 24);
  if (day < 30) return `${day} day${day === 1 ? "" : "s"} ago`;
  return formatDate(d);
}

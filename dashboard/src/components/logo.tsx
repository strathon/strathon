// Strathon mark — three concentric rings. Uses currentColor so it adapts to
// light/dark automatically (inherits the text color of wherever it sits).
// Traced from the brand PNG: radii 6.97 / 5.05 / 3.06 on a 24px box, stroke ~0.82.

export function StrathonLogo({ size = 24, className, style }: { size?: number; className?: string; style?: React.CSSProperties }) {
  const sw = 1.26; // stroke width in viewBox units — matches brand mark
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" className={className} style={style} aria-hidden="true">
      <circle cx="12" cy="12" r="10.57" stroke="currentColor" strokeWidth={sw} />
      <circle cx="12" cy="12" r="7.66" stroke="currentColor" strokeWidth={sw} />
      <circle cx="12" cy="12" r="4.61" stroke="currentColor" strokeWidth={sw} />
    </svg>
  );
}

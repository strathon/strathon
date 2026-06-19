// Strathon mark — three concentric rings. Uses currentColor so it adapts to
// light/dark automatically (inherits the text color of wherever it sits).
// Measured from the brand asset logo-mark.png: on a 100-unit box the radii are
// 47.1 / 34.2 / 20.6 (ratio 2.286 : 1.660 : 1.000), stroke 5.3. Scaled to this
// 24px box that is r = 11.30 / 8.21 / 4.94, stroke 1.27. Same source of truth
// as the website mark and favicon, so the logo is identical across surfaces.

export function StrathonLogo({ size = 24, className, style }: { size?: number; className?: string; style?: React.CSSProperties }) {
  const sw = 1.27; // stroke width in viewBox units — matches brand mark
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" className={className} style={style} aria-hidden="true">
      <circle cx="12" cy="12" r="11.30" stroke="currentColor" strokeWidth={sw} />
      <circle cx="12" cy="12" r="8.21" stroke="currentColor" strokeWidth={sw} />
      <circle cx="12" cy="12" r="4.94" stroke="currentColor" strokeWidth={sw} />
    </svg>
  );
}

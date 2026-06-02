import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Strathon — AI Agent Firewall",
  description: "Runtime policy enforcement and governance for AI agents",
};

// Resolve the stored theme (or the OS default) before first paint so there
// is no flash of the wrong palette. Kept in sync with src/lib/theme.ts.
const THEME_SCRIPT = `(function(){try{var k=localStorage.getItem("strathon-theme");var p=(k==="light"||k==="dark"||k==="system")?k:"system";var dark=p==="dark"||(p==="system"&&window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches);document.documentElement.dataset.theme=dark?"dark":"light";}catch(e){document.documentElement.dataset.theme="dark";}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <meta name="color-scheme" content="light dark" />
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}

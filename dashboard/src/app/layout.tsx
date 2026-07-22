import type { Metadata } from "next";
import { headers } from "next/headers";
import "./globals.css";

export const metadata: Metadata = {
  title: "Strathon — AI Agent Firewall",
  description: "Runtime policy enforcement and governance for AI agents",
};

// Resolve the stored theme (or the OS default) before first paint so there
// is no flash of the wrong palette. Kept in sync with src/lib/theme.ts.
const THEME_SCRIPT = `(function(){try{var k=localStorage.getItem("strathon-theme");var p=(k==="light"||k==="dark"||k==="system")?k:"system";var dark=p==="dark"||(p==="system"&&window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches);document.documentElement.dataset.theme=dark?"dark":"light";}catch(e){document.documentElement.dataset.theme="dark";}})();`;

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // The nonce is minted per request by src/proxy.ts. Under the strict CSP
  // (script-src 'nonce-...' 'strict-dynamic') an inline script without the
  // matching nonce is refused by the browser -- which is exactly what stops
  // an injected <script>, and equally what would stop this one. Reading the
  // header here opts the app into dynamic rendering: every page is
  // authenticated and fetches live data from the receiver, so there is no
  // static output being given up.
  const nonce = (await headers()).get("x-nonce") ?? undefined;

  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <meta name="color-scheme" content="light dark" />
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}

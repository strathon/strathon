import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Strathon — AI Agent Firewall",
  description: "Runtime policy enforcement and governance for AI agents",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="dark" suppressHydrationWarning>
      <body>{children}</body>
    </html>
  );
}

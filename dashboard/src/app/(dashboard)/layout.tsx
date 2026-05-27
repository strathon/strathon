import { Suspense } from "react";
import { DashboardShell } from "./layout.client";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const mode = (process.env.STRATHON_MODE || "self-hosted") as "self-hosted" | "cloud";
  return (
    <Suspense fallback={null}>
      <DashboardShell mode={mode}>{children}</DashboardShell>
    </Suspense>
  );
}

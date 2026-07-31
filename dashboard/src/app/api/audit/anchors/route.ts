import { proxyGetMapped } from "@/lib/api-proxy";
import { mapAuditAnchors } from "@/lib/transforms";

// Chain-level integrity status for the audit log header. The receiver's
// per-project anchor-status endpoint returns only whether the log is anchored
// and when last -- not the instance-wide anchor chain, which is admin-only.
export async function GET(req: Request) {
  return proxyGetMapped("/v1/audit/anchors/status", req, mapAuditAnchors);
}

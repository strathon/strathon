import { proxyGetMapped } from "@/lib/api-proxy";
import { mapAuditAnchors } from "@/lib/transforms";

// Chain-level integrity status for the audit log header. Proxies the receiver's
// anchors endpoint (periodic Merkle roots) and reduces it to the most recent
// anchor, which commits every event up to its sequence. Loaded once on page
// open — the standing "is this log tamper-evident" signal, distinct from the
// per-entry verify drill-down.
export async function GET(req: Request) {
  return proxyGetMapped("/v1/audit/anchors", req, mapAuditAnchors);
}

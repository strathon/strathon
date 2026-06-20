import { proxyGetMapped } from "@/lib/api-proxy";
import { mapAuditVerify } from "@/lib/transforms";

// Per-event integrity check. Proxies the receiver's hash-chain verify endpoint,
// which recomputes the entry's HMAC and returns a pass/fail verdict (never the
// raw hash — that stays server-side). Called when a row is inspected, so the
// list view stays one request regardless of row count.
export async function GET(req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxyGetMapped(`/v1/audit/events/${id}/verify`, req, mapAuditVerify);
}

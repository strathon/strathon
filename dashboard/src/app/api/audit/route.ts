import { proxyGetMapped } from "@/lib/api-proxy";
import { mapAudit } from "@/lib/transforms";

// The receiver exposes audit events at /v1/audit/events (the /v1/audit
// router prefix + /events), not /v1/audit. Map the nested actor/resource
// shape to the flat fields the page reads.
export async function GET(req: Request) {
  return proxyGetMapped("/v1/audit/events", req, mapAudit);
}

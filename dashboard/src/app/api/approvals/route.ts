import { proxyGetMapped } from "@/lib/api-proxy";
import { mapApprovals } from "@/lib/transforms";

export async function GET(req: Request) {
  return proxyGetMapped("/v1/approvals", req, mapApprovals);
}

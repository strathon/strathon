import { proxyGetMapped } from "@/lib/api-proxy";
import { mapTraces } from "@/lib/transforms";

export async function GET(req: Request) {
  return proxyGetMapped("/v1/traces", req, mapTraces);
}

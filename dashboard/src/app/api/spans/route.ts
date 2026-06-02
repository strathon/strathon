import { proxyGetMapped } from "@/lib/api-proxy";
import { mapSpans } from "@/lib/transforms";

export async function GET(req: Request) {
  return proxyGetMapped("/v1/spans", req, mapSpans);
}

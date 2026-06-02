import { proxyGetMapped } from "@/lib/api-proxy";
import { mapAgents } from "@/lib/transforms";

export async function GET(req: Request) {
  return proxyGetMapped("/v1/agents", req, mapAgents);
}

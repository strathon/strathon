import { proxyToReceiver } from "@/lib/api-proxy";
export async function GET() {
  return proxyToReceiver("/v1/ownership-transfers/pending", { method: "GET" });
}

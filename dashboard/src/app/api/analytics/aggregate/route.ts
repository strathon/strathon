import { proxyGet } from "@/lib/api-proxy";

// Pass-through to the receiver's span aggregation endpoint. The client
// pivots rows (dimension, bucket, span_count, total_cost_usd) as needed.
export async function GET(req: Request) {
  return proxyGet("/v1/spans/aggregate", req);
}

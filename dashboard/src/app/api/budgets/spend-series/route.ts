import { proxyGetMapped } from "@/lib/api-proxy";
import { mapCostSeries } from "@/lib/transforms";

// Per-agent daily spend for the budgets chart. The grouping is fixed (agent,
// day) so the receiver returns exactly the shape mapCostSeries pivots into a
// stacked area. Any caller-supplied range params on the request are still
// forwarded by the proxy.
export async function GET(req: Request) {
  return proxyGetMapped("/v1/costs?group_by=agent&period=day", req, mapCostSeries);
}

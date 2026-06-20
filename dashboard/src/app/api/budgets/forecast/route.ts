import { proxyGetMapped } from "@/lib/api-proxy";
import { mapBudgetForecast } from "@/lib/transforms";

export async function GET(req: Request) {
  return proxyGetMapped("/v1/costs/forecast", req, mapBudgetForecast);
}

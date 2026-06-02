import { proxyGetMapped, proxyMutateMapped } from "@/lib/api-proxy";
import { mapBudgets } from "@/lib/transforms";

export async function GET(req: Request) {
  return proxyGetMapped("/v1/budgets", req, mapBudgets);
}

// Map the dashboard's budget form to the receiver's CreateBudgetRequest.
// A budget is either a cost budget (max_spend_usd + budget_duration) or an
// iteration budget (max_repeated_calls + loop_window_seconds) — exactly one.
export async function POST(req: Request) {
  return proxyMutateMapped("/v1/budgets", req, (b) => {
    const out: Record<string, unknown> = {
      name: b.name,
      scope: b.scope ?? "project",
    };
    if (b.description != null) out.description = b.description;
    if (b.scope_value != null) out.scope_value = b.scope_value;
    if (b.kind === "iteration") {
      out.max_repeated_calls = b.max_repeated_calls;
      if (b.loop_window_seconds != null) out.loop_window_seconds = String(b.loop_window_seconds);
    } else {
      // cost budget (default)
      if (b.max_spend_usd != null) out.max_spend_usd = String(b.max_spend_usd);
      if (b.budget_duration != null) out.budget_duration = b.budget_duration;
      if (b.soft_limit_ratio != null) out.soft_limit_ratio = String(b.soft_limit_ratio);
    }
    return out;
  });
}

import { proxyGetMapped, proxyMutateMapped } from "@/lib/api-proxy";
import { mapPolicies } from "@/lib/transforms";

export async function GET(req: Request) {
  return proxyGetMapped("/v1/policies", req, mapPolicies);
}

// The new-policy form speaks { name, description, cel, action, status,
// priority }; the receiver requires { name, match_expression, action } plus
// enabled/shadow. Translate here so the page stays in its own vocabulary.
export async function POST(req: Request) {
  return proxyMutateMapped("/v1/policies", req, (b) => {
    const status = String(b.status ?? "enabled");
    const out: Record<string, unknown> = {
      name: b.name,
      match_expression: b.match_expression ?? b.cel,
      action: b.action,
      enabled: status !== "disabled",
      shadow: status === "shadow",
    };
    if (b.description != null) out.description = b.description;
    if (b.priority != null) out.priority = b.priority;
    if (b.action_config != null) out.action_config = b.action_config;
    if (b.applies_to != null) out.applies_to = b.applies_to;
    return out;
  });
}

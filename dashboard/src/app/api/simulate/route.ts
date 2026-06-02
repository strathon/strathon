import { proxyMutateMappedBoth } from "@/lib/api-proxy";
import { mapSimulate } from "@/lib/transforms";

// Dry-run a CEL expression against recent spans without creating a policy.
// Maps the editor's { cel } -> receiver's { match_expression } on the way in,
// and the receiver's { summary, matches } -> { evaluated, would_flag } out.
export async function POST(req: Request) {
  return proxyMutateMappedBoth(
    "/v1/policies/simulate",
    req,
    (b) => {
      const out: Record<string, unknown> = { match_expression: b.match_expression ?? b.cel };
      if (b.applies_to != null) out.applies_to = b.applies_to;
      if (b.start_after != null) out.start_after = b.start_after;
      return out;
    },
    mapSimulate,
  );
}

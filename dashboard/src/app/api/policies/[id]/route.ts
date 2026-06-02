import { proxyToReceiver, proxyGetMapped, proxyMutateMapped } from "@/lib/api-proxy";
import { mapPolicyDetail } from "@/lib/transforms";

export async function GET(req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxyGetMapped(`/v1/policies/${id}`, req, mapPolicyDetail);
}

// The editor sends { name, status, action, priority, cel }; translate to the
// receiver's { enabled, shadow, match_expression } contract.
export async function PATCH(req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxyMutateMapped(`/v1/policies/${id}`, req, (b) => {
    const out: Record<string, unknown> = {};
    if (b.name != null) out.name = b.name;
    if (b.action != null) out.action = b.action;
    if (b.priority != null) out.priority = b.priority;
    if (b.description != null) out.description = b.description;
    if (b.cel != null || b.match_expression != null) out.match_expression = b.match_expression ?? b.cel;
    if (b.status != null) { out.enabled = b.status !== "disabled"; out.shadow = b.status === "shadow"; }
    return out;
  }, "PATCH");
}

export async function DELETE(_: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxyToReceiver(`/v1/policies/${id}`, { method: "DELETE" });
}

import { proxyGetMapped } from "@/lib/api-proxy";
import { mapTraceTree } from "@/lib/transforms";

export async function GET(req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxyGetMapped(`/v1/traces/${id}/tree`, req, mapTraceTree);
}

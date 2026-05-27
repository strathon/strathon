import { proxyMutate, proxyToReceiver } from "@/lib/api-proxy";
export async function PATCH(req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxyMutate(`/v1/members/${id}`, req, "PATCH");
}
export async function DELETE(_: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxyToReceiver(`/v1/members/${id}`, { method: "DELETE" });
}

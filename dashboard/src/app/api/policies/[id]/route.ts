import { proxyToReceiver, proxyMutate } from "@/lib/api-proxy";
export async function GET(_: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxyToReceiver(`/v1/policies/${id}`);
}
export async function PATCH(req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxyMutate(`/v1/policies/${id}`, req, "PATCH");
}
export async function DELETE(_: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxyToReceiver(`/v1/policies/${id}`, { method: "DELETE" });
}

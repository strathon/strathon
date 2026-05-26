import { proxyToReceiver } from "@/lib/api-proxy";
export async function POST(_: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxyToReceiver(`/v1/members/${id}/reset-password`, { method: "POST" });
}

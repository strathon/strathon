import { proxyToReceiver } from "@/lib/api-proxy";
export async function POST(req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const url = new URL(req.url);
  const action = url.searchParams.get("action") || "approve";
  return proxyToReceiver(`/v1/approvals/${id}/${action}`, { method: "POST" });
}

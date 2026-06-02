import { proxyToReceiver } from "@/lib/api-proxy";

// Rotate an API key: issues a new secret and invalidates the old one.
export async function POST(_: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxyToReceiver(`/v1/api_keys/${id}/rotate`, { method: "POST" });
}

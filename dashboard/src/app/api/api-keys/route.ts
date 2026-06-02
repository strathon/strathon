import { proxyGetMapped, proxyMutate, proxyToReceiver } from "@/lib/api-proxy";
import { mapApiKeys } from "@/lib/transforms";

export async function GET(req: Request) { return proxyGetMapped("/v1/api_keys", req, mapApiKeys); }
export async function POST(req: Request) { return proxyMutate("/v1/api_keys", req); }
export async function DELETE(req: Request) {
  const url = new URL(req.url);
  const id = url.searchParams.get("id");
  if (!id) return Response.json({ error: { message: "Missing id" } }, { status: 400 });
  return proxyToReceiver(`/v1/api_keys/${id}`, { method: "DELETE" });
}

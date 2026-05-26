import { proxyMutate } from "@/lib/api-proxy";
export async function POST(req: Request) {
  const url = new URL(req.url);
  const action = url.searchParams.get("action") || "enable";
  return proxyMutate(`/v1/auth/mfa/${action}`, req);
}

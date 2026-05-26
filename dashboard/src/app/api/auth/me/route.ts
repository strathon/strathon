import { proxyToReceiver, clearSessionCookie } from "@/lib/api-proxy";
export async function GET() { return proxyToReceiver("/v1/auth/me"); }
export async function DELETE() {
  const res = await proxyToReceiver("/v1/auth/me", { method: "DELETE" });
  if (res.status === 200) await clearSessionCookie();
  return res;
}

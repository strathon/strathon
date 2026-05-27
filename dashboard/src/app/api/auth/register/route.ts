import { proxyToReceiver, setSessionCookie } from "@/lib/api-proxy";

export async function POST(request: Request) {
  const body = await request.json();
  const res = await proxyToReceiver("/v1/auth/register", {
    method: "POST",
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (res.status === 201 || (res.status === 200 && data.token)) {
    await setSessionCookie(data.token);
    return Response.json({ success: true });
  }
  return Response.json(data, { status: res.status });
}

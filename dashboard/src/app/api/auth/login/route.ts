import { proxyToReceiver, setSessionCookie } from "@/lib/api-proxy";

export async function POST(request: Request) {
  const body = await request.json();
  
  // Forward login to receiver.
  const res = await proxyToReceiver("/v1/auth/login", {
    method: "POST",
    body: JSON.stringify(body),
  });

  const data = await res.json();

  if (res.status === 200 && data.token) {
    // Set httpOnly cookie with the session token.
    await setSessionCookie(data.token);
    // Don't send the raw token to the browser.
    return Response.json({
      success: true,
      mfa_required: data.mfa_required || false,
      mfa_token: data.mfa_token || null,
    });
  }

  // Forward error as-is.
  return Response.json(data, { status: res.status });
}

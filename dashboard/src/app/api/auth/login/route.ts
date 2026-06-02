import { cookies } from "next/headers";
import { sessionCookieOptions } from "@/lib/cookies";

const RECEIVER_URL = process.env.RECEIVER_URL || "http://localhost:4318";

export async function POST(request: Request) {
  const body = await request.json();

  // The login page posts here for both steps: the password step ({email,
  // password}) and the MFA step ({mfa_token, code}). Route the MFA step to
  // the verify endpoint; otherwise hit the password login endpoint.
  const isMfaStep = !!(body?.mfa_token && body?.code);
  const target = isMfaStep ? "/v1/auth/mfa/verify" : "/v1/auth/login";

  const res = await fetch(`${RECEIVER_URL}${target}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  const data = await res.json().catch(() => null);

  // Password step with MFA enabled: no token yet, return the challenge.
  if (!isMfaStep && res.status === 200 && data?.mfa_required) {
    return Response.json({
      success: false,
      mfa_required: true,
      mfa_token: data.mfa_token || null,
    });
  }

  if (res.status === 200 && data?.token) {
    const cookieStore = await cookies();

    // Set session cookie.
    cookieStore.set("strathon-session", data.token, sessionCookieOptions());

    // Fetch /auth/me to get project_id for the X-Project-Id header.
    try {
      const meRes = await fetch(`${RECEIVER_URL}/v1/auth/me`, {
        headers: { Authorization: `Bearer ${data.token}` },
      });
      if (meRes.ok) {
        const meData = await meRes.json();
        const projectId = meData?.user?.project_id;
        if (projectId) {
          cookieStore.set("strathon-project-id", projectId, sessionCookieOptions());
        }
      }
    } catch {
      // Non-fatal: project context will be missing, some API calls may fail.
    }

    return Response.json({ success: true, mfa_required: false, mfa_token: null });
  }

  return Response.json(data, { status: res.status });
}

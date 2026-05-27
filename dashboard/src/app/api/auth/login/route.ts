import { cookies } from "next/headers";

const RECEIVER_URL = process.env.RECEIVER_URL || "http://localhost:4318";

export async function POST(request: Request) {
  const body = await request.json();

  // Forward login to receiver.
  const res = await fetch(`${RECEIVER_URL}/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  const data = await res.json();

  if (res.status === 200 && data.token) {
    const cookieStore = await cookies();

    // Set session cookie.
    cookieStore.set("strathon-session", data.token, {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "strict",
      path: "/",
      maxAge: 86400,
    });

    // Fetch /auth/me to get project_id for the X-Project-Id header.
    try {
      const meRes = await fetch(`${RECEIVER_URL}/v1/auth/me`, {
        headers: { Authorization: `Bearer ${data.token}` },
      });
      if (meRes.ok) {
        const meData = await meRes.json();
        const projectId = meData?.user?.project_id;
        if (projectId) {
          cookieStore.set("strathon-project-id", projectId, {
            httpOnly: true,
            secure: process.env.NODE_ENV === "production",
            sameSite: "strict",
            path: "/",
            maxAge: 86400,
          });
        }
      }
    } catch {
      // Non-fatal: project context will be missing, some API calls may fail.
    }

    return Response.json({
      success: true,
      mfa_required: data.mfa_required || false,
      mfa_token: data.mfa_token || null,
    });
  }

  return Response.json(data, { status: res.status });
}

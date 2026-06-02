import { cookies } from "next/headers";
import { sessionCookieOptions } from "@/lib/cookies";

const RECEIVER_URL = process.env.RECEIVER_URL || "http://localhost:4318";

export async function POST(request: Request) {
  const body = await request.json();

  const res = await fetch(`${RECEIVER_URL}/v1/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  const data = await res.json();

  if ((res.status === 201 || res.status === 200) && data.token) {
    const cookieStore = await cookies();

    cookieStore.set("strathon-session", data.token, sessionCookieOptions());

    // Fetch /auth/me for project_id.
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
    } catch {}

    return Response.json({ success: true });
  }

  return Response.json(data, { status: res.status });
}

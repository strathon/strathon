import { cookies } from "next/headers";

const RECEIVER_URL = process.env.RECEIVER_URL || "http://localhost:4318";

/**
 * POST /api/projects/switch  { project_id }
 *
 * Sets the strathon-project-id cookie to the chosen project. We verify the
 * caller is actually a member of that project (via /v1/auth/me) before
 * switching, so a forged project_id can't grant access — the receiver also
 * enforces membership on every subsequent call, this is just a fast guard.
 */
export async function POST(request: Request) {
  const cookieStore = await cookies();
  const session = cookieStore.get("strathon-session")?.value;
  if (!session) {
    return Response.json({ error: { message: "Not authenticated" } }, { status: 401 });
  }

  const body = await request.json().catch(() => null);
  const projectId: string | undefined = body?.project_id;
  if (!projectId) {
    return Response.json({ error: { message: "project_id is required" } }, { status: 400 });
  }

  // Confirm membership before switching.
  let memberships: Array<{ id?: string }> = [];
  try {
    const meRes = await fetch(`${RECEIVER_URL}/v1/auth/me`, {
      headers: { Authorization: `Bearer ${session}` },
      cache: "no-store",
    });
    const meData = await meRes.json().catch(() => null);
    memberships = Array.isArray(meData?.projects) ? meData.projects : [];
  } catch {
    return Response.json({ error: { message: "Receiver unreachable" } }, { status: 502 });
  }

  if (!memberships.some((p) => p?.id === projectId)) {
    return Response.json({ error: { message: "Not a member of that project" } }, { status: 403 });
  }

  cookieStore.set("strathon-project-id", projectId, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict",
    path: "/",
    maxAge: 86400,
  });

  return Response.json({ success: true, project_id: projectId });
}

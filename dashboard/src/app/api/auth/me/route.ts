import { cookies } from "next/headers";
import { sessionCookieOptions } from "@/lib/cookies";
import { proxyToReceiver, clearSessionCookie } from "@/lib/api-proxy";

const RECEIVER_URL = process.env.RECEIVER_URL || "http://localhost:4318";

/**
 * GET /api/auth/me
 *
 * Returns the current user and, as a side effect, keeps the
 * strathon-project-id cookie in step with the user's membership. A user
 * who was invited to a project after logging in (or before they had an
 * account) has a valid session cookie but no project-id cookie yet; left
 * alone, every other API call would lack project context. Setting the
 * cookie here means the dashboard heals itself on the next load without a
 * forced re-login.
 */
export async function GET() {
  const cookieStore = await cookies();
  const session = cookieStore.get("strathon-session")?.value;
  if (!session) {
    return Response.json({ error: { message: "Not authenticated" } }, { status: 401 });
  }

  let res: Response;
  try {
    res = await fetch(`${RECEIVER_URL}/v1/auth/me`, {
      headers: { Authorization: `Bearer ${session}` },
      cache: "no-store",
    });
  } catch {
    return Response.json({ error: { message: "Receiver unreachable" } }, { status: 502 });
  }

  const data = await res.json().catch(() => null);

  if (res.ok && data?.user) {
    const current = cookieStore.get("strathon-project-id")?.value;
    const memberships: Array<{ id?: string; name?: string }> = Array.isArray(data.projects) ? data.projects : [];
    const isMemberOfCurrent = !!current && memberships.some((p) => p?.id === current);
    const primaryId: string | null = data.user.project_id ?? null;

    let activeId: string | null = null;
    if (isMemberOfCurrent) {
      // Keep the user's chosen project (set by the switcher); don't force primary.
      activeId = current!;
    } else if (primaryId) {
      // No cookie, or it points at a project they're no longer in — heal to primary.
      cookieStore.set("strathon-project-id", primaryId, sessionCookieOptions());
      activeId = primaryId;
    } else if (current) {
      // No memberships at all — drop the stale project cookie.
      cookieStore.delete("strathon-project-id");
    }

    // Reflect the *active* project (cookie-driven) back on the user object so
    // the dashboard's "current project" matches what the receiver sees via
    // X-Project-Id. The receiver always reports projects[0] here otherwise.
    if (activeId) {
      const activeProject = memberships.find((p) => p?.id === activeId);
      data.user.project_id = activeId;
      if (activeProject?.name) data.user.project_name = activeProject.name;
      if (activeProject && "role" in activeProject) data.user.role = (activeProject as { role?: string }).role ?? data.user.role;
    }
  }

  return Response.json(data, { status: res.status });
}

export async function DELETE() {
  const res = await proxyToReceiver("/v1/auth/me", { method: "DELETE" });
  if (res.status === 200) await clearSessionCookie();
  return res;
}

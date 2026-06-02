import { cookies } from "next/headers";
import { sessionCookieOptions } from "@/lib/cookies";

const RECEIVER_URL = process.env.RECEIVER_URL || "http://localhost:4318";

/**
 * POST /api/projects  { name, slug }
 *
 * Creates a project. The receiver enrolls the creating user as its owner.
 * On success we switch the active-project cookie to the new project so the
 * user lands in it immediately. Cookies are mutated before the response is
 * built (Next 16 disallows cookie mutation after a Response is constructed).
 */
export async function POST(request: Request) {
  const cookieStore = await cookies();
  const session = cookieStore.get("strathon-session")?.value;
  if (!session) {
    return Response.json({ error: { message: "Not authenticated" } }, { status: 401 });
  }
  const projectId = cookieStore.get("strathon-project-id")?.value;
  const body = await request.json().catch(() => null);

  let res: Response;
  try {
    res = await fetch(`${RECEIVER_URL}/v1/projects`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${session}`,
        ...(projectId ? { "X-Project-Id": projectId } : {}),
      },
      body: JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    return Response.json({ error: { message: "Receiver unreachable" } }, { status: 502 });
  }

  const data = await res.json().catch(() => null);
  if (res.ok && data?.id) {
    cookieStore.set("strathon-project-id", data.id, sessionCookieOptions());
  }
  return Response.json(data, { status: res.status });
}

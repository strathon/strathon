import { cookies } from "next/headers";
import { sessionCookieOptions } from "@/lib/cookies";

const RECEIVER_URL = process.env.RECEIVER_URL || "http://localhost:4318";

export async function proxyToReceiver(path: string, init?: RequestInit & { searchParams?: URLSearchParams }): Promise<Response> {
  const cookieStore = await cookies();
  const session = cookieStore.get("strathon-session")?.value;
  if (!session && !path.startsWith("/v1/auth/")) {
    return Response.json({ error: { message: "Not authenticated" } }, { status: 401 });
  }
  const url = new URL(path, RECEIVER_URL);
  if (init?.searchParams) init.searchParams.forEach((v, k) => url.searchParams.set(k, v));
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (session) headers["Authorization"] = `Bearer ${session}`;
  const projectId = cookieStore.get("strathon-project-id")?.value;
  if (projectId) headers["X-Project-Id"] = projectId;
  try {
    const res = await fetch(url.toString(), { ...init, headers: { ...headers, ...init?.headers }, cache: "no-store" });
    // 204/205/304 must have a null body — constructing a Response with any
    // body for these statuses throws, which previously surfaced as a bogus
    // "receiver unreachable" on successful deletes (the receiver returns 204).
    if (res.status === 204 || res.status === 205 || res.status === 304) {
      return new Response(null, { status: res.status });
    }
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) return Response.json(await res.json(), { status: res.status });
    return new Response(await res.blob(), { status: res.status, headers: ct ? { "Content-Type": ct } : undefined });
  } catch {
    return Response.json({ error: { message: "Receiver unreachable" } }, { status: 502 });
  }
}

export async function proxyGet(path: string, request: Request): Promise<Response> {
  const url = new URL(request.url);
  return proxyToReceiver(path, { method: "GET", searchParams: url.searchParams });
}

/**
 * GET proxy that reshapes the receiver's JSON before returning it. The
 * dashboard pages read a normalized shape (typically { data: [...] });
 * the receiver speaks its own resource-specific shape. The map function
 * bridges the two so pages render real data. Non-2xx responses pass
 * through untouched so error handling still works.
 */
export async function proxyGetMapped(
  path: string,
  request: Request,
  map: (body: unknown) => unknown,
): Promise<Response> {
  const url = new URL(request.url);
  const res = await proxyToReceiver(path, { method: "GET", searchParams: url.searchParams });
  if (!res.ok) return res;
  let body: unknown = null;
  try { body = await res.json(); } catch { return res; }
  try {
    return Response.json(map(body), { status: res.status });
  } catch {
    // If a transform throws, fall back to the raw body rather than 500.
    return Response.json(body, { status: res.status });
  }
}

export async function proxyMutate(path: string, request: Request, method = "POST"): Promise<Response> {
  const body = await request.text();
  return proxyToReceiver(path, { method, body: body || undefined });
}

/**
 * Mutating proxy that reshapes the request body before forwarding. Lets a
 * page send its natural shape (e.g. { cel, status }) while the receiver
 * receives its required shape (e.g. { match_expression, enabled, shadow }).
 */
export async function proxyMutateMapped(
  path: string,
  request: Request,
  map: (body: Record<string, unknown>) => Record<string, unknown>,
  method = "POST",
): Promise<Response> {
  const raw = await request.text();
  let parsed: Record<string, unknown> = {};
  try { parsed = raw ? JSON.parse(raw) : {}; } catch { parsed = {}; }
  const mapped = map(parsed);
  return proxyToReceiver(path, { method, body: JSON.stringify(mapped) });
}

/**
 * Mutating proxy that maps BOTH the request body (on the way in) and the
 * response body (on the way out). Used where the page and the receiver
 * differ on both sides, e.g. policy simulation.
 */
export async function proxyMutateMappedBoth(
  path: string,
  request: Request,
  mapRequest: (body: Record<string, unknown>) => Record<string, unknown>,
  mapResponse: (body: unknown) => unknown,
  method = "POST",
): Promise<Response> {
  const raw = await request.text();
  let parsed: Record<string, unknown> = {};
  try { parsed = raw ? JSON.parse(raw) : {}; } catch { parsed = {}; }
  const res = await proxyToReceiver(path, { method, body: JSON.stringify(mapRequest(parsed)) });
  if (!res.ok) return res;
  let body: unknown = null;
  try { body = await res.json(); } catch { return res; }
  try { return Response.json(mapResponse(body), { status: res.status }); }
  catch { return Response.json(body, { status: res.status }); }
}

export async function setSessionCookie(token: string): Promise<void> {
  const cookieStore = await cookies();
  cookieStore.set("strathon-session", token, sessionCookieOptions());
}

export async function clearSessionCookie(): Promise<void> {
  const cookieStore = await cookies();
  cookieStore.delete("strathon-session");
  cookieStore.delete("strathon-project-id");
}

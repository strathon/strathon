import { cookies } from "next/headers";

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
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) return Response.json(await res.json(), { status: res.status });
    return new Response(await res.blob(), { status: res.status, headers: { "Content-Type": ct } });
  } catch {
    return Response.json({ error: { message: "Receiver unreachable" } }, { status: 502 });
  }
}

export async function proxyGet(path: string, request: Request): Promise<Response> {
  const url = new URL(request.url);
  return proxyToReceiver(path, { method: "GET", searchParams: url.searchParams });
}

export async function proxyMutate(path: string, request: Request, method = "POST"): Promise<Response> {
  const body = await request.text();
  return proxyToReceiver(path, { method, body: body || undefined });
}

export async function setSessionCookie(token: string): Promise<void> {
  const cookieStore = await cookies();
  cookieStore.set("strathon-session", token, {
    httpOnly: true, secure: process.env.NODE_ENV === "production",
    sameSite: "strict", path: "/", maxAge: 86400,
  });
}

export async function clearSessionCookie(): Promise<void> {
  const cookieStore = await cookies();
  cookieStore.delete("strathon-session");
  cookieStore.delete("strathon-project-id");
}

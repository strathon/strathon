import { proxyToReceiver } from "@/lib/api-proxy";

// Soft-delete a project. The receiver refuses to delete the last remaining
// project (409). We don't touch the project cookie here — after the client
// reloads, /api/auth/me heals it: if the deleted project was current, the
// user is no longer a member of it, so the cookie is dropped and re-pointed
// at a remaining project automatically.
export async function DELETE(_: Request, { params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  return proxyToReceiver(`/v1/projects/${encodeURIComponent(slug)}`, { method: "DELETE" });
}

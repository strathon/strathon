import { proxyToReceiver } from "@/lib/api-proxy";
import { mapApprovals } from "@/lib/transforms";

// The receiver filters with `status_filter` and accepts a single status
// (pending|approved|denied|expired). The dashboard's two tabs are
// "pending" (one status — filter server-side) and "resolved" (everything
// that is no longer pending — fetch all, filter here).
export async function GET(req: Request) {
  const url = new URL(req.url);
  const status = url.searchParams.get("status");
  const searchParams = new URLSearchParams();
  const limit = url.searchParams.get("limit");
  if (limit) searchParams.set("limit", limit);
  if (status === "pending") searchParams.set("status_filter", "pending");

  const res = await proxyToReceiver("/v1/approvals", { method: "GET", searchParams });
  if (!res.ok) return res;
  const mapped = mapApprovals(await res.json()) as { data: Array<{ status?: string }> };
  if (status === "resolved") {
    mapped.data = mapped.data.filter((a) => a.status && a.status !== "pending");
  }
  return Response.json(mapped);
}

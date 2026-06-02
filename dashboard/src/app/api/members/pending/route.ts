import { proxyGet } from "@/lib/api-proxy";

// Pending invitations — people invited but not yet registered. Surfaced in
// the members list so an owner can see outstanding invites.
export async function GET(req: Request) {
  return proxyGet("/v1/members/pending", req);
}

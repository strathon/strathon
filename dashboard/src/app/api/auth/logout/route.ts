import { clearSessionCookie } from "@/lib/api-proxy";

export async function POST() {
  await clearSessionCookie();
  return Response.json({ success: true });
}

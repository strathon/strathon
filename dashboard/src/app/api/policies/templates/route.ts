import { proxyGet } from "@/lib/api-proxy";

// Surfaces the receiver's OWASP-mapped policy template catalog to the
// dashboard "New policy" gallery.
export async function GET(req: Request) {
  return proxyGet("/v1/policy-templates", req);
}

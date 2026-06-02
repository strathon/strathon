import { proxyMutate } from "@/lib/api-proxy";

// Revoke an outstanding invitation before it is redeemed.
export async function DELETE(req: Request, { params }: { params: Promise<{ email: string }> }) {
  const { email } = await params;
  return proxyMutate(`/v1/members/pending/${encodeURIComponent(email)}`, req, "DELETE");
}

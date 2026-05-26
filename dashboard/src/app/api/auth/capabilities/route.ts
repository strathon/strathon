import { proxyToReceiver } from "@/lib/api-proxy";
export async function GET() { return proxyToReceiver("/v1/auth/capabilities"); }

import { proxyGet } from "@/lib/api-proxy";
export async function GET(req: Request) { return proxyGet("/v1/agents", req); }

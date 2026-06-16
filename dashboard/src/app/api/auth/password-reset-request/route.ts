import { proxyMutate } from "@/lib/api-proxy";
export async function POST(req: Request) { return proxyMutate("/v1/auth/reset-password/request", req); }

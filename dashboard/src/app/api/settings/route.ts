import { proxyGet, proxyMutate } from "@/lib/api-proxy";
export async function GET(req: Request) { return proxyGet("/v1/settings", req); }
export async function PATCH(req: Request) { return proxyMutate("/v1/settings", req, "PATCH"); }

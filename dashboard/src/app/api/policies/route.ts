import { proxyGet, proxyMutate } from "@/lib/api-proxy";
export async function GET(req: Request) { return proxyGet("/v1/policies", req); }
export async function POST(req: Request) { return proxyMutate("/v1/policies", req); }

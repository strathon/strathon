import { proxyMutate, proxyGet } from "@/lib/api-proxy";
export async function GET(req: Request) { return proxyGet("/v1/compliance/sarif", req); }
export async function POST(req: Request) { return proxyMutate("/v1/compliance/export", req); }

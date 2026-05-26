import { proxyGet, proxyMutate } from "@/lib/api-proxy";
export async function GET(req: Request) { return proxyGet("/v1/halts", req); }
export async function POST(req: Request) { return proxyMutate("/v1/halts", req); }
export async function DELETE(req: Request) { return proxyMutate("/v1/halts", req, "DELETE"); }

import { proxyMutate } from "@/lib/api-proxy";
import { mapCompliance } from "@/lib/transforms";

// No framework-coverage endpoint exists on the receiver, so the dashboard's
// coverage list is intentionally empty (honest empty state, not fabricated
// percentages). Compliance reporting runs through export/SARIF below.
export async function GET() {
  return Response.json(mapCompliance());
}
export async function POST(req: Request) { return proxyMutate("/v1/compliance/export", req); }

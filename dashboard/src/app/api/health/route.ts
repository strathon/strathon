const RECEIVER_URL = process.env.RECEIVER_URL || "http://localhost:4318";

export async function GET() {
  try {
    const res = await fetch(`${RECEIVER_URL}/health`, { cache: "no-store" });
    const data = await res.json();
    return Response.json(data);
  } catch {
    return Response.json({ status: "unreachable" }, { status: 502 });
  }
}

import { proxyMutate } from "@/lib/api-proxy";

// General data export (manual download). Forwards the dataset selection,
// time range, and format to the receiver, which returns a JSON document or
// a ZIP of per-dataset CSVs. The receiver streams the bytes back through
// the proxy; the page sets the download filename.
export async function POST(req: Request) {
  return proxyMutate("/v1/export", req);
}

import { redirect } from "next/navigation";
// API keys now live under Settings. Keep this route working for old links.
export default function ApiKeysRedirect() { redirect("/settings?section=apikeys"); }

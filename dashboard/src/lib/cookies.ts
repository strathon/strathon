// Centralized cookie options for auth/session cookies.
//
// The `secure` flag is gated behind STRATHON_COOKIE_SECURE rather than
// NODE_ENV. A self-hosted deployment is built with NODE_ENV=production but is
// commonly accessed over plain HTTP (http://localhost:3000, or an internal
// host before a TLS terminator is added). Browsers refuse to store `Secure`
// cookies sent over HTTP, which silently breaks login. Defaulting `secure`
// off makes self-host work out of the box; operators serving over HTTPS set
// STRATHON_COOKIE_SECURE=true (recommended in production behind a reverse
// proxy).

const SECURE = process.env.STRATHON_COOKIE_SECURE === "true";

export const SESSION_COOKIE = "strathon-session";
export const PROJECT_COOKIE = "strathon-project-id";

export function sessionCookieOptions() {
  return {
    httpOnly: true,
    secure: SECURE,
    sameSite: "lax" as const,
    path: "/",
    maxAge: 86400,
  };
}

// The selected-project cookie. Same flags as the session cookie, and that
// matters in two ways:
//
//   `secure` must follow STRATHON_COOKIE_SECURE, not NODE_ENV. A self-hosted
//   image is built with NODE_ENV=production but is usually served over plain
//   HTTP, and browsers silently drop Secure cookies on HTTP -- which would
//   make project switching quietly fail to persist on the default deployment.
//
//   `sameSite` must match the session cookie. Strathon links straight into
//   the dashboard from Slack approval notifications; on that cross-site
//   navigation a `strict` cookie is withheld while the `lax` session cookie
//   is sent, so the page would load authenticated but with no project context.
export function projectCookieOptions() {
  return {
    httpOnly: true,
    secure: SECURE,
    sameSite: "lax" as const,
    path: "/",
    maxAge: 86400,
  };
}

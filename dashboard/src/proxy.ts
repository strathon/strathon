/**
 * Next.js proxy (formerly "middleware") - runs before every request.
 *
 * 1. Gates all dashboard routes behind session cookie
 * 2. Blocks CVE-2025-29927 (x-middleware-subrequest bypass)
 * 3. Issues a per-request CSP nonce and adds security headers
 *
 * The proxy is NOT the security boundary. Next.js has shipped several
 * middleware/proxy bypasses (CVE-2025-29927, and segment-prefetch bypasses
 * in the 16.x line), so the cookie check below is a routing convenience,
 * not an authorization check. Every /api/* route re-verifies the session
 * against the receiver, and the receiver authenticates every request
 * independently -- that is where authorization actually happens.
 *
 * Research: Next.js 16 CSP guide (nonce + strict-dynamic), Google/OWASP
 * strict-CSP guidance, CVE-2025-29927 middleware bypass (CVSS 9.1).
 */

import { NextRequest, NextResponse } from "next/server";

const PUBLIC_PATHS = [
  "/login", "/register", "/forgot-password", "/reset-password",
  "/api/auth/login", "/api/auth/register", "/api/auth/capabilities",
  "/api/auth/password-reset-request", "/api/auth/password-reset",
  "/api/version", "/favicon.ico",
];

/**
 * Per-request nonce. Uses Web Crypto, not Node's Buffer: the proxy can run
 * in the Edge runtime, where Buffer does not exist.
 */
function generateNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

/**
 * True when this request arrived over TLS. Behind a reverse proxy the
 * connection to Next is plain HTTP, so x-forwarded-proto is the signal.
 */
function isHttps(request: NextRequest): boolean {
  const forwarded = request.headers.get("x-forwarded-proto");
  if (forwarded) return forwarded.split(",")[0].trim() === "https";
  return request.nextUrl.protocol === "https:";
}

function buildCsp(nonce: string, https: boolean): string {
  const isDev = process.env.NODE_ENV === "development";

  const directives = [
    "default-src 'self'",

    // Strict CSP: the nonce authorizes Next's inline bootstrap and the
    // theme script in layout.tsx; 'strict-dynamic' lets those nonced
    // scripts load the app's own chunks without enumerating every hash.
    // An injected <script> carries no nonce, so the browser refuses it --
    // this is the directive that actually stops XSS. 'self' is kept as a
    // fallback for browsers predating strict-dynamic (which ignore it).
    // React's dev overlay needs eval; production never does.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${isDev ? " 'unsafe-eval'" : ""}`,

    // React `style={{...}}` props compile to inline style attributes, and
    // style attributes cannot carry a nonce. Style injection is not script
    // execution, so 'unsafe-inline' here is the standard trade-off every
    // React app makes; script-src above stays strict.
    "style-src 'self' 'unsafe-inline'",

    // data: for the MFA QR code, blob: for client-generated downloads.
    "img-src 'self' blob: data:",
    "font-src 'self'",

    // The dashboard only ever talks to its own /api/* proxy routes.
    "connect-src 'self'",

    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    // Supersedes X-Frame-Options for modern browsers; both are sent.
    "frame-ancestors 'none'",
  ];

  // Only on TLS. On a plain-HTTP self-host, upgrade-insecure-requests would
  // rewrite same-origin subresource URLs to https:// and break every asset
  // load -- a white-screen dashboard. Self-host over HTTP is a first-class
  // deployment here (see src/lib/cookies.ts for the same reasoning).
  if (https) directives.push("upgrade-insecure-requests");

  return directives.join("; ");
}

export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Block CVE-2025-29927: middleware bypass via x-middleware-subrequest.
  if (request.headers.get("x-middleware-subrequest")) {
    return new NextResponse(null, { status: 403 });
  }

  const nonce = generateNonce();
  const https = isHttps(request);
  const csp = buildCsp(nonce, https);

  // Forward the nonce and the policy on the REQUEST headers. Next.js reads
  // the Content-Security-Policy request header and stamps the nonce onto the
  // framework's own inline scripts; layout.tsx reads x-nonce for the theme
  // script. Without this, a strict script-src would block Next's bootstrap
  // and the page would render blank.
  const proceed = () => {
    const requestHeaders = new Headers(request.headers);
    requestHeaders.set("x-nonce", nonce);
    requestHeaders.set("Content-Security-Policy", csp);
    return addSecurityHeaders(
      NextResponse.next({ request: { headers: requestHeaders } }),
      csp,
      https,
    );
  };

  // Allow public paths.
  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
    return proceed();
  }

  // Allow static assets and Next.js internals.
  if (
    pathname.startsWith("/_next/") ||
    pathname.startsWith("/api/auth/") ||
    pathname.endsWith(".ico") ||
    pathname.endsWith(".png") ||
    pathname.endsWith(".svg")
  ) {
    return proceed();
  }

  // Check session cookie for all other routes.
  const session = request.cookies.get("strathon-session");
  if (!session?.value) {
    // API routes return 401, page routes redirect to login.
    if (pathname.startsWith("/api/")) {
      return addSecurityHeaders(
        NextResponse.json(
          { error: { message: "Not authenticated" } },
          { status: 401 }
        ),
        csp,
        https,
      );
    }
    const loginUrl = new URL("/login", request.url);
    loginUrl.searchParams.set("redirect", pathname);
    return addSecurityHeaders(NextResponse.redirect(loginUrl), csp, https);
  }

  return proceed();
}

function addSecurityHeaders(
  response: NextResponse,
  csp: string,
  https: boolean,
): NextResponse {
  response.headers.set("Content-Security-Policy", csp);
  response.headers.set("X-Content-Type-Options", "nosniff");
  response.headers.set("X-Frame-Options", "DENY");
  response.headers.set("X-XSS-Protection", "0");
  response.headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  response.headers.set(
    "Permissions-Policy",
    "camera=(), microphone=(), geolocation=()"
  );

  // HSTS only over TLS. Browsers ignore it on plain HTTP anyway, but sending
  // it unconditionally would pin any operator who once served HTTPS on this
  // host into HTTPS-only for a year -- a nasty surprise for a self-hoster who
  // later reverts to HTTP. 1 year + includeSubDomains is the OWASP baseline;
  // `preload` is deliberately omitted (an irreversible commitment the
  // operator, not Strathon, must opt into).
  if (https) {
    response.headers.set(
      "Strict-Transport-Security",
      "max-age=31536000; includeSubDomains"
    );
  }

  // Don't cache authenticated pages.
  if (!response.headers.has("Cache-Control")) {
    response.headers.set(
      "Cache-Control",
      "no-store, no-cache, must-revalidate"
    );
  }
  return response;
}

export const config = {
  matcher: [
    // Match all paths except static files.
    "/((?!_next/static|_next/image|favicon.ico).*)",
  ],
};

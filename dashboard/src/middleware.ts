/**
 * Next.js middleware — runs before every request.
 *
 * 1. Gates all dashboard routes behind session cookie
 * 2. Blocks CVE-2025-29927 (x-middleware-subrequest bypass)
 * 3. Adds security headers to every response
 *
 * Research: Next.js security best practices 2026, CVE-2025-29927
 * middleware bypass (CVSS 9.1), OWASP secure headers project.
 */

import { NextRequest, NextResponse } from "next/server";

const PUBLIC_PATHS = [
  "/login", "/register", "/forgot-password", "/reset-password",
  "/api/auth/login", "/api/auth/register", "/api/auth/capabilities",
  "/api/auth/password-reset-request", "/api/auth/password-reset",
  "/api/version", "/favicon.ico",
];

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Block CVE-2025-29927: middleware bypass via x-middleware-subrequest.
  if (request.headers.get("x-middleware-subrequest")) {
    return new NextResponse(null, { status: 403 });
  }

  // Allow public paths.
  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
    return addSecurityHeaders(NextResponse.next());
  }

  // Allow static assets and Next.js internals.
  if (
    pathname.startsWith("/_next/") ||
    pathname.startsWith("/api/auth/") ||
    pathname.endsWith(".ico") ||
    pathname.endsWith(".png") ||
    pathname.endsWith(".svg")
  ) {
    return addSecurityHeaders(NextResponse.next());
  }

  // Check session cookie for all other routes.
  const session = request.cookies.get("strathon-session");
  if (!session?.value) {
    // API routes return 401, page routes redirect to login.
    if (pathname.startsWith("/api/")) {
      return NextResponse.json(
        { error: { message: "Not authenticated" } },
        { status: 401 }
      );
    }
    const loginUrl = new URL("/login", request.url);
    loginUrl.searchParams.set("redirect", pathname);
    return NextResponse.redirect(loginUrl);
  }

  return addSecurityHeaders(NextResponse.next());
}

function addSecurityHeaders(response: NextResponse): NextResponse {
  response.headers.set("X-Content-Type-Options", "nosniff");
  response.headers.set("X-Frame-Options", "DENY");
  response.headers.set("X-XSS-Protection", "0");
  response.headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  response.headers.set(
    "Permissions-Policy",
    "camera=(), microphone=(), geolocation=()"
  );
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

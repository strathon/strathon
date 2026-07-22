# Strathon Dashboard

Operator UI for [Strathon](https://github.com/strathon/strathon), the
open-source AI agent firewall. Next.js App Router frontend that talks to the
receiver API; ships in the Docker Compose stack.

## Run

```bash
npm install
npm run dev
```

Open http://localhost:3000 — redirects to /overview. The dashboard expects a
running receiver (default `http://localhost:4318`; override with
`RECEIVER_URL`). Set `STRATHON_COOKIE_SECURE=true` only when serving over
HTTPS.

## Stack

- Next.js 16 (App Router) + TypeScript, Turbopack
- No Tailwind — the canonical design system lives in `src/styles/prototype.css`
- `lucide-react` for icons, `clsx`
- All data comes from the receiver via the `/api/*` proxy routes in
  `src/app/api/` (session cookie auth); response shapes are normalized in
  `src/lib/transforms.ts`

## Structure

```
src/
  app/
    layout.tsx                 root <html data-theme>
    page.tsx                   redirect → /overview
    login/page.tsx
    api/                       proxy routes to the receiver
    (dashboard)/
      layout.client.tsx        .app grid shell, theme, hotkeys, breadcrumbs
      overview/                landing: spend trend, liveness, recent activity
      policies/                list + [id]/ CEL editor + simulator
      traces/                  list + [id]/ waterfall / flame / graph
      spans/                   search + FTS
      approvals/  agents/  audit/  budgets/  compliance/
      apikeys/                 redirect stub -> /settings?section=apikeys
      settings/                7 sections incl. API keys (show-once secrets)
  components/
    icons.tsx                  lucide re-exports under prototype names
    ui.tsx                     shared primitives
    shell.tsx                  Sidebar, Header, UserMenu, CommandPalette
  lib/
    transforms.ts              receiver-response mappers
    hooks.ts
  styles/
    prototype.css              canonical design tokens + component CSS
```

## Notes

- Theme toggle (dark/light) via `data-theme` on `<html>` (UserMenu +
  Settings → Appearance).
- Settings deep-links via `?section=export` etc.
- All exports live in Settings → Export.

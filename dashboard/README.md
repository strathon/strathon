# Strathon Dashboard

Production Next.js port of the Strathon AI-agent-firewall dashboard.

## Run

```bash
npm install
npm run dev
```

Open http://localhost:3000 — redirects to /policies.

## Stack

- Next.js 16 (App Router) + TypeScript, Turbopack
- No Tailwind — the prototype's canonical CSS lives verbatim in `src/styles/prototype.css`
- `lucide-react` for icons, `clsx`
- Typed mock data in `src/lib/mock-data.ts` (swap for the receiver API later)

## Structure

```
src/
  app/
    layout.tsx                 root <html data-theme>
    page.tsx                   redirect → /policies
    login/page.tsx
    (dashboard)/
      layout.tsx               Suspense wrapper
      layout.client.tsx        .app grid shell, theme, hotkeys, breadcrumbs
      policies/page.tsx        + [id]/page.tsx (CEL editor + simulator)
      traces/page.tsx          + [id]/page.tsx (waterfall / flame / graph)
      spans/page.tsx
      approvals/page.tsx
      agents/page.tsx
      audit/page.tsx
      budgets/page.tsx
      compliance/page.tsx
      settings/page.tsx        + settings-client.tsx (7 sections)
      apikeys/page.tsx
  components/
    icons.tsx                  lucide re-exports under prototype names
    ui.tsx                     all 33 shared primitives
    shell.tsx                  Sidebar, Header, UserMenu, CommandPalette, MobileNav
  lib/
    mock-data.ts
    hooks.ts
  styles/
    prototype.css              canonical design tokens + component CSS
```

## Notes

- Theme toggle (dark/light) is wired via `data-theme` on `<html>` (UserMenu + Settings → Appearance).
- Settings deep-links via `?section=export` etc.
- All exports live only in Settings → Export.
- `Slack` icon maps to `lucide`'s `MessageSquare` (Slack glyph not in this lucide version) — swap if you add a custom one.

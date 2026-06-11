# Role-Based Access Control (RBAC)

Strathon supports two authentication methods:

1. **API keys** — for SDK telemetry ingestion and programmatic access
2. **Session tokens** — for dashboard users with email/password auth

## Roles

Every dashboard user has a **role** per project that determines their permissions:

| Role | Description |
|------|-------------|
| **owner** | Full access. Can delete project, manage all members. |
| **admin** | Full access except project deletion. Can manage non-owner members. |
| **operator** | Read/write on policies, halts, budgets, webhooks, traces. Cannot manage members or API keys. |
| **viewer** | Read-only access to all resources. |

### Role hierarchy

Roles are strictly ordered: **owner > admin > operator > viewer**.

You can only assign or modify roles below your own rank. Owners can manage admins, admins can manage operators and viewers, etc.

## Authentication

### Register the first user

```bash
curl -X POST http://localhost:4318/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@example.com", "password": "your-secure-password", "display_name": "Admin"}'
```

The first user to register automatically becomes **owner** of the default project.

### Login

```bash
curl -X POST http://localhost:4318/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@example.com", "password": "your-secure-password"}'
```

Returns a session token. Use it in the `Authorization` header:

```
Authorization: Bearer <session-token>
```

### Current user

```bash
curl http://localhost:4318/v1/auth/me \
  -H "Authorization: Bearer <session-token>"
```

### Logout

```bash
curl -X POST http://localhost:4318/v1/auth/logout \
  -H "Authorization: Bearer <session-token>"
```

## Membership management

Session-authenticated users with **owner** or **admin** role can manage project members.

### List members

```bash
curl http://localhost:4318/v1/projects/default/members \
  -H "Authorization: Bearer <session-token>" \
  -H "X-Project-Id: <project-uuid>"
```

### Add a member

```bash
curl -X POST http://localhost:4318/v1/projects/default/members \
  -H "Authorization: Bearer <session-token>" \
  -H "X-Project-Id: <project-uuid>" \
  -H "Content-Type: application/json" \
  -d '{"email": "operator@example.com", "role": "operator"}'
```

### Change a member's role

```bash
curl -X PATCH http://localhost:4318/v1/projects/default/members/<user-id> \
  -H "Authorization: Bearer <session-token>" \
  -H "X-Project-Id: <project-uuid>" \
  -H "Content-Type: application/json" \
  -d '{"role": "viewer"}'
```

### Remove a member

```bash
curl -X DELETE http://localhost:4318/v1/projects/default/members/<user-id> \
  -H "Authorization: Bearer <session-token>" \
  -H "X-Project-Id: <project-uuid>"
```

## Session auth + project context

Session tokens are user-scoped (one token, multiple projects). When using session auth with endpoints that need a project context, provide the `X-Project-Id` header:

```
X-Project-Id: <project-uuid>
```

API keys don't need this header — they're already project-scoped.

## API keys

SDK and programmatic API keys are separate from dashboard roles: they use capability-based scopes, not roles. See [api_keys.md](api_keys.md) for details.

## Multi-factor authentication (TOTP)

Dashboard users can enable TOTP-based MFA on their account. Four endpoints,
all under `/v1/auth`:

| Endpoint | What it does |
|----------|--------------|
| `POST /v1/auth/mfa/setup` | Generates a TOTP secret for the current user (session auth). Returns the base32 secret and an `otpauth://` URI for QR scanning. |
| `POST /v1/auth/mfa/verify-setup` | Verifies a TOTP code and enables MFA. Returns one-time backup codes — store them safely. |
| `POST /v1/auth/mfa/verify` | Completes an MFA login: takes the short-lived `mfa_token` from the login response plus a TOTP or backup code, returns a full session token. |
| `POST /v1/auth/mfa/disable` | Disables MFA. Requires the current password and a valid TOTP code. |

Once MFA is enabled, `POST /v1/auth/login` with the correct password returns
an `mfa_token` instead of a session token; the client exchanges it at
`/v1/auth/mfa/verify` to finish signing in.

## Password security

Passwords are hashed with **Argon2id** using OWASP-recommended parameters:

- Memory: 46 MiB (m=47104)
- Iterations: 1 (t=1)
- Parallelism: 1 (p=1)

Parameters are stored in the hash string itself. If OWASP recommendations change, Strathon will transparently rehash on next login (`check_needs_rehash`).

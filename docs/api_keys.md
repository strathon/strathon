# API Keys

Strathon receivers authenticate requests via API keys passed in the standard
`Authorization: Bearer <key>` header. Each key is scoped to a single project
and resolves every request to that project.

## Key format

Strathon API keys look like:

```
stra_<your-key-shown-once-on-creation>
```

The `stra_` prefix identifies the scheme. The random part contains 256 bits of
entropy from `secrets.token_urlsafe(32)`. The first 12 characters of the key
serve as an indexed lookup prefix; the full key is verified via constant-time
SHA-256 comparison.

## Local development

For local development you can opt into a seeded development key by setting
`STRATHON_SEED_DEV_KEY=true` before the receiver runs its migrations. When
enabled, this key is seeded for the default project:

```
stra_dev_local_default_project_do_not_use_in_production
```

This is **off by default** and is **never seeded in cloud mode**, because the
key value is publicly known, so anyone with HTTP access to your receiver could
use it. Enable it only for local development and demos. On any shared or
production deployment, leave it off and create a real key instead (see
"Creating a real API key" below). If you did enable it and later move to
production, rotate it (see "Rotating the dev key").

## Creating a real API key

Key management is scope-gated (`api_keys:read` / `api_keys:write`). The
seeded dev key carries the wildcard scope, so on a fresh deployment you use
it to mint your first real key, then revoke it:

```bash
curl -X POST http://localhost:4318/v1/api_keys \
  -H "Authorization: Bearer $STRATHON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "production deploy 2026-05"}'
```

The response includes a `key` field exactly once. Save it, because it cannot be
retrieved later (only the SHA-256 hash is stored).

```json
{
  "id": "2a9943e3-97fc-497b-9c15-8458a1feaf36",
  "project_id": "00000000-0000-0000-0000-000000000001",
  "name": "production deploy 2026-05",
  "key_prefix": "stra_xxxxxxx",
  "key": "stra_xxxx...xxxx",
  "created_at": "2026-05-14T11:57:15.153470+00:00",
  "last_used_at": null,
  "revoked_at": null
}
```

Use this key in your SDK initialization:

```python
from strathon import Client

client = Client(
    api_key="stra_your_api_key_here",
    endpoint="https://strathon.your-domain.com",
)
```

## Listing keys

```bash
curl http://localhost:4318/v1/api_keys \
  -H "Authorization: Bearer $STRATHON_API_KEY"
```

Returns key metadata (`id`, `name`, `key_prefix`, timestamps) but never the
raw key. Use `?include_revoked=true` to also see revoked keys.

## Revoking a key

```bash
curl -X DELETE http://localhost:4318/v1/api_keys/<key-id> \
  -H "Authorization: Bearer $STRATHON_API_KEY"
```

Soft-revokes the key by setting `revoked_at`. Subsequent requests using the
revoked key get a 401. The key is not deleted from the database (audit trail).

## Rotating the dev key

Before any production deployment, do this:

1. Create a real API key (see above) and save the raw value somewhere safe.
2. Update your SDK config to use the new key.
3. Revoke the seeded dev key:

   ```bash
   curl -X DELETE http://localhost:4318/v1/api_keys/00000000-0000-0000-0000-000000000010 \
     -H "Authorization: Bearer $STRATHON_NEW_API_KEY"
   ```

   (Authenticate this call with the *new* key: once the dev key is revoked,
   it can no longer authorize anything, including its own revocation if you
   get the order wrong. Revoking with the new key avoids the chicken-and-egg.)

After step 3, the well-known dev key no longer works. Anyone who knew it can
no longer act as your default project.

## Scopes

Every key carries a list of capability scopes; each endpoint requires a
specific scope (`/v1/api_keys` requires `api_keys:read` for GET and
`api_keys:write` for POST/DELETE). The seeded dev key has the wildcard `*`;
production keys should be minted with only the scopes they need. The one
operational consequence of the well-known dev key: until you rotate it,
anyone with HTTP access to your receiver can act as the default project:
which is why rotation is step one of any shared or production deployment.

## What happens on auth failure

| Situation                                   | Response                                              |
|---------------------------------------------|-------------------------------------------------------|
| No `Authorization` header                   | `401 Missing or malformed Authorization header`       |
| Bearer token doesn't match `stra_<prefix>`  | `401 Invalid API key`                                 |
| Key prefix in DB but hash doesn't match     | `401 Invalid API key`                                 |
| Key was revoked                             | `401 Invalid API key`                                 |
| Valid key                                   | Resolves to `project_id`, updates `last_used_at`      |

All failure responses are intentionally identical to avoid leaking which
prefixes exist in the database.

## Related

- [RBAC](rbac.md): dashboard roles, sessions, and MFA
- [Projects](projects.md): every key is scoped to one project
- [Audit log](audit.md): key creation and revocation are recorded

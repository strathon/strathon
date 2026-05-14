# API Keys

Strathon receivers authenticate requests via API keys passed in the standard
`Authorization: Bearer <key>` header. Each key is scoped to a single project
and resolves every request to that project.

## Key format

Strathon API keys look like:

```
stra_aB3xC9zD2eF1gH4iJ6kL8mN0oP2qR4sT6uV8wX0yZ
```

The `stra_` prefix identifies the scheme. The random part contains 256 bits of
entropy from `secrets.token_urlsafe(32)`. The first 12 characters of the key
serve as an indexed lookup prefix; the full key is verified via constant-time
SHA-256 comparison.

## Local development

The default Strathon Postgres schema includes a seeded development key for
the default project:

```
stra_dev_local_default_project_do_not_use_in_production
```

This key is hard-coded into the migration so demos work out of the box. **It
is not a secret** — anyone with HTTP access to your receiver can use it.
Rotate immediately when moving to a shared or production deployment (see
"Rotating the dev key" below).

## Creating a real API key

```bash
curl -X POST http://localhost:4318/v1/api_keys \
  -H "Content-Type: application/json" \
  -d '{"name": "production deploy 2026-05"}'
```

The response includes a `key` field exactly once. Save it — it cannot be
retrieved later (only the SHA-256 hash is stored).

```json
{
  "id": "2a9943e3-97fc-497b-9c15-8458a1feaf36",
  "project_id": "00000000-0000-0000-0000-000000000001",
  "name": "production deploy 2026-05",
  "key_prefix": "stra_66l4KiV",
  "key": "stra_66l4KiV6GXSqqtm1MLvgTNYdku180BwnItDGmMKZ5YM",
  "created_at": "2026-05-14T11:57:15.153470+00:00",
  "last_used_at": null,
  "revoked_at": null
}
```

Use this key in your SDK initialization:

```python
from strathon import Client

client = Client(
    api_key="stra_66l4KiV6GXSqqtm1MLvgTNYdku180BwnItDGmMKZ5YM",
    endpoint="https://strathon.your-domain.com",
)
```

## Listing keys

```bash
curl http://localhost:4318/v1/api_keys
```

Returns key metadata (`id`, `name`, `key_prefix`, timestamps) but never the
raw key. Use `?include_revoked=true` to also see revoked keys.

## Revoking a key

```bash
curl -X DELETE http://localhost:4318/v1/api_keys/<key-id>
```

Soft-revokes the key by setting `revoked_at`. Subsequent requests using the
revoked key get a 401. The key is not deleted from the database (audit trail).

## Rotating the dev key

Before any production deployment, do this:

1. Create a real API key (see above) and save the raw value somewhere safe.
2. Update your SDK config to use the new key.
3. Revoke the seeded dev key:

   ```bash
   curl -X DELETE http://localhost:4318/v1/api_keys/00000000-0000-0000-0000-000000000010
   ```

After step 3, the well-known dev key no longer works. Anyone who knew it can
no longer act as your default project.

## v1 limitations

The `/v1/api_keys` endpoints are themselves UNAUTHENTICATED. This is
acceptable for local development but unsafe for production. Until v2 adds
proper admin authentication, you MUST:

- Run the receiver on a private network (Tailscale, VPN, internal VPC), OR
- Put a reverse proxy (nginx, Caddy, Cloudflare Access) in front of it
  that restricts `/v1/api_keys/*` to admin sessions

Public-internet receivers with no proxy in front of them will leak project
membership and allow anyone to mint new keys.

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

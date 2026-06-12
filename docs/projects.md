# Projects

Strathon is multi-tenant. Every resource (traces, policies, API keys,
budgets, halts) belongs to a project. The receiver ships with a seeded
`default` project; the projects API lets operators create more.

## Creating a project

```http
POST /v1/projects
Content-Type: application/json
Authorization: Bearer stra_…

{"name": "Production Agents", "slug": "prod-agents"}
```

Returns:

```json
{
  "id": "...",
  "name": "Production Agents",
  "slug": "prod-agents",
  "api_key": "stra_…",
  "api_key_scopes": ["traces:write", "policies:read"]
}
```

Creating a project also creates its `project_settings` row (with
defaults) and mints an initial API key with SDK-default scopes. The
key plaintext is returned in the response and is never stored: record
it immediately.

Slug rules: 3–64 characters, lowercase alphanumeric plus hyphens,
cannot start or end with a hyphen. Duplicate slugs return 409.

## Listing projects

```http
GET /v1/projects
Authorization: Bearer stra_…
```

Returns all non-deleted projects. Add `?include_deleted=true` to
include soft-deleted projects.

## Getting a project

```http
GET /v1/projects/{slug}
Authorization: Bearer stra_…
```

Returns the project with resource counts (active API keys, policies,
traces).

## Updating a project

```http
PATCH /v1/projects/{slug}
Content-Type: application/json
Authorization: Bearer stra_…

{"name": "Renamed Project"}
```

Only the name is updatable. Slugs are immutable after creation.

## Deleting a project

```http
DELETE /v1/projects/{slug}
Authorization: Bearer stra_…
```

Soft-deletes the project (sets `deleted_at`). The project no longer
appears in list results and its slug cannot be reused. Data is
retained for audit purposes.

## Scope

All project management endpoints require `projects:manage` scope.
The wildcard scope (`*`) includes it.

## Related

- [RBAC](rbac.md): per-project roles and membership
- [API keys](api_keys.md): keys resolve to exactly one project
- [Retention](retention.md): retention is configured per project

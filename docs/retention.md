# Retention

Strathon automatically deletes traces older than each project's configured
retention window. The deletion cascades through FK constraints — removing
a trace removes its spans, span_events, span_links, and policy_matches in
one operation.

## How it works

A single background task runs inside the receiver process. On a fixed
interval it queries `project_settings.trace_retention_days` for every
project and deletes traces whose `start_time_unix_nano` is older than
`now - retention_days`.

The default retention window is **30 days** (from the schema default in
`project_settings.trace_retention_days`). Per-project overrides are stored
in that same column.

## Configuration

| Env var                                  | Default | Meaning                                                   |
|------------------------------------------|---------|-----------------------------------------------------------|
| `STRATHON_RETENTION_ENABLED`             | `true`  | Set to `false` (or `0`, `no`, `off`) to disable the loop. |
| `STRATHON_RETENTION_INTERVAL_SECONDS`    | `3600`  | Seconds between sweeps. Floored at 60.                    |
| `STRATHON_RETENTION_BATCH_SIZE`          | `5000`  | Max traces deleted per project per sweep.                 |

The receiver logs its effective config at startup:

```
Retention loop starting: interval=3600s, batch_size=5000
```

## Why a cap on batch size

Each sweep deletes at most `batch_size` traces per project. Deletes hold
row locks until commit; an unbounded `DELETE FROM traces WHERE …` on a
busy project would block concurrent ingest INSERTs on those rows.

If a project has more than `batch_size` expired traces (e.g. you just
shrunk retention from 90 days to 7 and there's a huge backlog), the
remainder catches up over subsequent sweeps. With the default
`batch_size=5000` and `interval=3600s` that's 120,000 expired traces/day
of throughput, plenty for any realistic backlog scenario.

## Disabling per project

Set `trace_retention_days` to 0 via the REST API to retain that project's
traces forever:

```http
PATCH /v1/project/settings
Content-Type: application/json
Authorization: Bearer stra_…

{"trace_retention_days": 0}
```

The retention loop skips projects with `trace_retention_days = 0` entirely.

## REST API

Retention is configured via the project settings endpoints:

```http
GET /v1/project/settings
Authorization: Bearer stra_…
```

Returns `{"intervention_default_action": "allow", "trace_retention_days": 30}`.

```http
PATCH /v1/project/settings
Content-Type: application/json
Authorization: Bearer stra_…

{"trace_retention_days": 90}
```

Valid range: 1–3650 days. Set to 0 to disable retention for the project.
Requires `project_settings:write` scope.

## Multi-process / horizontal scaling caveat

The retention loop runs inside each receiver process. If you scale out
with multiple `uvicorn` workers or multiple replicas, every process runs
its own sweep. The deletes are idempotent (a `DELETE WHERE id IN (...)`
on already-deleted rows is a no-op) so correctness is preserved, but
you'll see redundant DB load.

For multi-process deployments, the cleanest fix is to:

1. Disable the loop on the receiver: `STRATHON_RETENTION_ENABLED=false`
2. Run retention as a separate scheduled job (cron, systemd timer,
   Kubernetes CronJob) that calls a one-shot Python script wrapping
   `retention.cleanup_once()`.

v2 will add Postgres advisory-lock-based leader election so one of N
running processes performs each sweep.

## Monitoring

The retention loop exports three Prometheus counters via `/metrics`:

```
strathon_receiver_retention_sweeps_total
strathon_receiver_retention_traces_deleted_total
strathon_receiver_retention_sweep_errors_total
```

A healthy receiver should show `sweeps_total` incrementing on the
configured interval and `sweep_errors_total` staying at 0. If
`traces_deleted_total` is climbing rapidly, your retention window may
be too short for ingest volume.

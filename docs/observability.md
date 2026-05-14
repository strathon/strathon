# Metrics and structured logging

The Strathon receiver exposes Prometheus-format metrics at `/metrics` and
can emit structured JSON logs for ingestion into Loki, Datadog, CloudWatch,
or any other log aggregator that parses NDJSON.

## Metrics

### Endpoint

```
GET /metrics
```

Returns text in the standard Prometheus exposition format. Unauthenticated
by design — Prometheus scrapers commonly run without credentials. Restrict
via network ACL or reverse proxy if you don't want it public.

Sample Prometheus scrape config:

```yaml
scrape_configs:
  - job_name: strathon-receiver
    metrics_path: /metrics
    static_configs:
      - targets: ['strathon-receiver:4318']
    scrape_interval: 15s
```

### Available metrics

All metrics live in the `strathon_receiver_` namespace.

#### Sampling

| Metric                                              | Type    | Description                                      |
|-----------------------------------------------------|---------|--------------------------------------------------|
| `strathon_receiver_sampling_spans_kept_total`       | counter | Spans persisted after sampling decision.         |
| `strathon_receiver_sampling_spans_dropped_total`    | counter | Spans dropped at ingest by sampling.             |
| `strathon_receiver_sampling_spans_force_kept_total` | counter | Spans kept by an always-keep rule that overrode a would-be drop (policy match, error, expensive LLM call). |
| `strathon_receiver_sampling_rate`                   | gauge   | Configured probabilistic sample rate (0.0–1.0).  |

#### Retention

| Metric                                                | Type    | Description                          |
|-------------------------------------------------------|---------|--------------------------------------|
| `strathon_receiver_retention_sweeps_total`            | counter | Completed retention sweeps.          |
| `strathon_receiver_retention_traces_deleted_total`    | counter | Traces deleted by retention sweeps.  |
| `strathon_receiver_retention_sweep_errors_total`      | counter | Retention sweeps that raised.        |

#### Policy enforcement

| Metric                                       | Labels    | Description                                    |
|----------------------------------------------|-----------|------------------------------------------------|
| `strathon_receiver_policy_matches_total`     | `action`  | Policy matches by action (`log`, `alert`, `block`, `steer`). |

#### Authentication

| Metric                                       | Type    | Description                                |
|----------------------------------------------|---------|--------------------------------------------|
| `strathon_receiver_auth_failures_total`      | counter | Requests rejected with 401.                |
| `strathon_receiver_auth_successes_total`     | counter | Successful API key authentications.        |

### Useful PromQL queries

```promql
# Spans persisted vs dropped, rate per minute
rate(strathon_receiver_sampling_spans_kept_total[1m])
rate(strathon_receiver_sampling_spans_dropped_total[1m])

# % of spans force-kept by safety rules
rate(strathon_receiver_sampling_spans_force_kept_total[5m])
  / rate(strathon_receiver_sampling_spans_kept_total[5m])

# Block rate by policy action
sum by (action) (rate(strathon_receiver_policy_matches_total[5m]))

# Auth failure rate (alert candidate)
rate(strathon_receiver_auth_failures_total[5m])
```

## Structured logging

By default the receiver logs human-readable text to stderr. Set
`STRATHON_LOG_FORMAT=json` to switch to one JSON object per line:

```bash
STRATHON_LOG_FORMAT=json \
DATABASE_URL=postgresql://... \
  uvicorn main:app --host 0.0.0.0 --port 4318
```

Each record contains:

| Field      | Always present | Notes                                                          |
|------------|----------------|----------------------------------------------------------------|
| `time`     | yes            | ISO-8601 with milliseconds, UTC.                               |
| `level`    | yes            | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.               |
| `logger`   | yes            | Logger name (`strathon.receiver`, `strathon.receiver.auth`, …).|
| `msg`      | yes            | Formatted message text.                                        |
| `exc_info` | only on errors | Multi-line stack trace as a single string.                     |
| `<extras>` | varies         | Any `extra={...}` kwargs the call site passed.                 |

For example, after ingesting a batch:

```json
{
  "time": "2026-05-14T13:08:27.510+00:00",
  "level": "INFO",
  "logger": "strathon.receiver",
  "msg": "Ingested 2 spans across 2 traces",
  "spans_ingested": 2,
  "traces_seen": 2,
  "project_id": "00000000-0000-0000-0000-000000000001"
}
```

uvicorn's access logs and error logs also get the JSON formatter applied
when this mode is on.

Set log verbosity with `STRATHON_LOG_LEVEL` (default `INFO`):

```bash
STRATHON_LOG_LEVEL=DEBUG STRATHON_LOG_FORMAT=json uvicorn main:app …
```

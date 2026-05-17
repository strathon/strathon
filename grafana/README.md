# Grafana Dashboard

Import `strathon-receiver.json` into your Grafana instance to get a
pre-built dashboard covering every metric the Strathon receiver
exposes at `/metrics`.

## Quick import

1. Open Grafana → Dashboards → Import
2. Upload `strathon-receiver.json` (or paste its contents)
3. Select your Prometheus data source when prompted
4. Click Import

## What's on the dashboard

| Row | Panels |
|-----|--------|
| Overview | Spans/sec, policy blocks/sec, cost tracked, budget violations, auth failures, rate limit rejections |
| Policy Enforcement | Matches by action (block/steer/throttle/log/alert), halts created vs cleared |
| Cost & Budget | Cost by model, budget evaluations/violations/errors |
| Webhook Delivery | Dispatch pipeline (dispatched/success/failure/DLQ), sweeper health |
| Sampling & Retention | Kept/dropped/force-kept, sampling rate gauge, retention sweeps |
| Authentication | Auth outcomes by scope, rate limit rejections by key type |

## Prometheus scrape config

Point Prometheus at the receiver's `/metrics` endpoint:

```yaml
scrape_configs:
  - job_name: strathon-receiver
    scrape_interval: 15s
    static_configs:
      - targets: ['localhost:4318']
    metrics_path: /metrics
```

All metrics are prefixed with `strathon_receiver_`.

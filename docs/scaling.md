# Scaling Guide

Strathon scales from a single-instance development setup to high-volume
production. This guide explains how, and — importantly — where the real
limit is.

## Measuring throughput on your hardware

Throughput depends heavily on your hardware, PostgreSQL configuration, and
span payload, so this guide does not quote a single universal number. To get
a figure you can trust for your environment, run the included benchmark:

```bash
# Start a receiver (4 uvicorn workers is a reasonable reference config):
cd receiver && uvicorn main:app --workers 4 --port 4318

# In another shell, drive load through the full ingestion pipeline:
pip install httpx opentelemetry-proto protobuf
python benchmarks/loadtest.py \
    --endpoint http://127.0.0.1:4318 \
    --api-key "$STRATHON_API_KEY" \
    --requests 5000 --concurrency 16 --batch-size 20
```

The harness exercises the complete per-span pipeline — OTLP protobuf parse,
CEL policy evaluation, credential pattern scan, PII redaction, and the batched
PostgreSQL write — and reports sustained spans/sec, requests/sec, latency
p50/p95/p99, and the error rate, alongside the hardware and config it ran on.
Re-run it after any tuning to see the effect. The numbers it prints are the
numbers to publish for your deployment; do not extrapolate.

## Horizontal scaling and where the limit is

Strathon receivers are stateless and scale horizontally: run N instances
behind a load balancer (round-robin or least-connections; receivers share no
in-memory state).

```
SDK → Load Balancer → Receiver 1 ┐
                    → Receiver 2 ├─→ PostgreSQL
                    → Receiver N ┘
```

The receiver tier scales close to linearly **until the shared PostgreSQL
becomes the bottleneck**, which it will: all receivers write to one primary
database. Past that point, adding receivers does not add throughput — you have
to scale the database. So plan capacity around the database write path, not the
receiver count:

- **Connection pressure** — front PostgreSQL with PgBouncer (see below) before
  adding many instances, or you will exhaust the connection limit.
- **Write IOPS / CPU** — a larger PostgreSQL instance (more CPU, faster disk)
  raises the ceiling more reliably than more receivers once the DB is hot.
- **Read load** — move dashboard/analytics reads to replicas (see below) so
  they don't compete with ingest writes on the primary.
- **Retention** — keep the live partition set bounded (monthly partitioning +
  detaching old partitions) so write performance doesn't degrade over time.

If you need throughput beyond what a single well-provisioned primary sustains,
the next step is partitioning writes across more than one database, which
Strathon's monthly-partitioned schema is structured for but which is a
deliberate operational step, not an automatic one. Benchmark your primary
first — most deployments are far from that ceiling.

## Connection Pooling with PgBouncer

At 10+ instances, use PgBouncer between the receivers and PostgreSQL
to avoid exhausting the database connection limit. The production
Docker Compose file includes PgBouncer pre-configured:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up
```

This routes all receiver connections through PgBouncer on port 6432.

Recommended PgBouncer settings:

- `pool_mode = transaction` (Strathon uses short transactions)
- `max_client_conn = 1000` (allow many receiver connections)
- `default_pool_size = 50` (connections to the actual database)

## Read Replicas

For dashboard-heavy workloads (many concurrent operators viewing
traces and analytics), add PostgreSQL read replicas:

- **Primary**: handles all writes from receivers
- **Replicas**: handle read queries from the dashboard

Configure the dashboard to use a read replica connection string.
The receiver always writes to the primary.

## Partitioned Spans

Strathon partitions the `spans` table by month using PostgreSQL RANGE
partitioning. This is automatic and requires no configuration.

Benefits at scale:

- Queries on recent data scan only the relevant partition
- Old partitions can be detached and archived without downtime
- Maintenance operations (VACUUM, ANALYZE) run per-partition

The partition naming convention is `spans_yYYYYmMM`. Strathon creates
future partitions automatically.

## Monitoring

At scale, monitor:

- **Receiver**: request latency (p50/p95/p99), error rate, queue depth
- **PostgreSQL**: connections, query latency, replication lag (if using replicas)
- **PgBouncer**: pool utilization, wait time

Strathon exposes a `/metrics` endpoint (Prometheus format) on each
receiver instance. Scrape this with Prometheus and visualize with
Grafana. A reference Grafana dashboard is included in `grafana/`.

## Recommendations by Scale

The receiver count is rarely the constraint; the database is. These are
starting points — benchmark your primary to size it.

| Scale | Setup |
|-------|-------|
| Development | Single `docker compose up` |
| Startup (< 1K agents) | 1-2 receiver instances, single PostgreSQL |
| Growth (1K-10K agents) | A few receiver instances, PgBouncer in front of PostgreSQL, scheduled partition maintenance, read replicas for the dashboard |
| High volume (10K+ agents) | Multiple receivers behind a load balancer, PgBouncer, a dedicated well-provisioned Postgres primary + read replicas. If a single primary's write path is the ceiling, partition writes across databases (a deliberate operational step). |

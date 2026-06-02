# Benchmarks

## loadtest.py

Measures end-to-end OTLP span ingestion throughput, latency (p50/p95/p99), and
error rate against a live receiver. Every request runs the full production
pipeline: OTLP protobuf parse, CEL policy evaluation, credential pattern scan,
PII redaction, and the batched PostgreSQL write.

The throughput figures in `docs/scaling.md` come from this script. Re-run it on
your hardware to reproduce or update them — it reports what your machine
sustained, with the hardware/config it ran on. Do not extrapolate published
numbers across different hardware.

```bash
# Start a receiver (4 uvicorn workers is a reasonable reference config):
docker compose up -d
# or: cd ../receiver && uvicorn main:app --workers 4 --port 4318

pip install httpx opentelemetry-proto protobuf

python loadtest.py --requests 5000 --concurrency 16 --batch-size 20
```

Exits non-zero if the error rate exceeds 1%, so it can gate a release check.

## Finding your real ceiling (sweep mode)

Throughput rises with concurrency until the database saturates, then plateaus.
To find that plateau in one command, sweep several concurrency levels:

```bash
python loadtest.py --requests 5000 --batch-size 20 --sweep 8,16,32,64
```

It runs the test at each level and prints a throughput curve marking the peak.
Where spans/sec stops climbing as concurrency rises is your real ceiling —
usually Postgres-bound, since all receiver workers share one database. Publish
the peak figure (with the hardware line) rather than a single arbitrary run.

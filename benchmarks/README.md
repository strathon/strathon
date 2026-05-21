# Strathon Benchmarks

## Load Test

Measures OTLP ingest throughput (spans/second) and request latency.

### Prerequisites

```bash
pip install httpx
docker compose up -d   # Start receiver + Postgres
```

### Run

```bash
# Default: 1000 requests × 50 spans = 50,000 spans, 10 concurrency
python benchmarks/loadtest.py

# High throughput test
python benchmarks/loadtest.py --requests 5000 --concurrency 50 --batch-size 100

# Against a remote instance
STRATHON_API_KEY=stra_... python benchmarks/loadtest.py --endpoint https://api.getstrathon.com
```

### Parameters

| Flag | Default | Description |
|------|---------|-------------|
| `--requests` | 1000 | Number of HTTP requests to send |
| `--concurrency` | 10 | Max concurrent requests |
| `--batch-size` | 50 | Spans per request |
| `--endpoint` | http://localhost:4318 | Receiver URL |
| `--api-key` | dev key | API key |

### Target

**10,000 spans/second** on a single receiver instance with Postgres.

### Sample Output

```
RESULTS
==================================================
  Requests:      1000
  Batch size:    50 spans/request
  Total spans:   50,000
  Wall time:     4.32s
  Successes:     1000
  Errors:        0
  Error rate:    0.0%

  Throughput:    11,574 spans/sec
  Requests/sec:  231

  Latency p50:   32.1ms
  Latency p95:   78.4ms
  Latency p99:   124.2ms
  Latency max:   201.8ms

  ✅ PASS: 11,574 spans/sec >= 10,000 target
```

### Tuning

If below target:
- Increase `--batch-size` (fewer HTTP requests, more spans per batch)
- Check Postgres: `shared_buffers`, `work_mem`, `max_connections`
- Check uvicorn workers: `--workers 4` for multi-core
- Check if PII redaction regexes are bottleneck (disable temporarily to measure)
- Check if policy evaluation count is high (many active policies × many spans)

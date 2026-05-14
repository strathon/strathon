# Self-hosting Strathon

Strathon ships as a docker-compose stack: Postgres plus the receiver in
two containers, sharing one network. From a fresh clone to a working
receiver is typically under 60 seconds (most of which is the first
Postgres image pull).

## Prerequisites

- Docker 24+ with the Compose plugin (`docker compose`, not the deprecated
  `docker-compose`)
- 200 MB of disk for the Postgres volume
- Ports 4318 (receiver) and 5432 (Postgres) free

## Standing it up

```bash
git clone https://github.com/strathon/strathon.git
cd strathon
docker compose up
```

On first start:

1. Postgres pulls and initializes
2. The migrations in `db/migrations/` run automatically against the empty
   database (`docker-entrypoint-initdb.d` mount)
3. The receiver builds and starts
4. The receiver detects the seeded dev API key and prints a quickstart
   banner with the key value, endpoint, and rotation hint

The banner looks like this:

```
============================================================
  Strathon receiver ready
============================================================
  Endpoint:   http://localhost:4318
  Dev API key (rotate before production!):
      stra_dev_local_default_project_do_not_use_in_production
...
============================================================
```

Once you see it, the receiver is ready for traffic.

## Verifying

```bash
# Health check
curl http://localhost:4318/health

# Authenticated request
curl -H "Authorization: Bearer stra_dev_local_default_project_do_not_use_in_production" \
  http://localhost:4318/v1/policies

# Prometheus metrics
curl http://localhost:4318/metrics
```

Or run one of the framework demos:

```bash
pip install strathon langchain cel-python
python examples/intervention_demo.py
```

## Configuration

All knobs are env vars. Copy `.env.example` to `.env` to override
defaults; the compose file picks it up automatically.

| Variable                              | Default          | Purpose                                |
|---------------------------------------|------------------|----------------------------------------|
| `POSTGRES_PASSWORD`                   | `strathon_dev`   | Postgres password.                     |
| `STRATHON_LOG_LEVEL`                  | `INFO`           | Receiver log verbosity.                |
| `STRATHON_LOG_FORMAT`                 | `text`           | `text` or `json` (one record per line).|
| `STRATHON_SAMPLING_RATE`              | `1.0`            | 0.0-1.0. See docs/sampling.md.         |
| `STRATHON_RETENTION_ENABLED`          | `true`           | Background trace cleanup.              |
| `STRATHON_RETENTION_INTERVAL_SECONDS` | `3600`           | Seconds between retention sweeps.      |
| `STRATHON_RETENTION_BATCH_SIZE`       | `5000`           | Max traces deleted per project/sweep.  |

## Lifecycle commands

```bash
# Start (detached)
docker compose up -d

# Tail receiver logs
docker compose logs -f receiver

# Stop, keep data
docker compose down

# Stop AND wipe the Postgres volume (fresh start)
docker compose down -v

# Rebuild after pulling new code
docker compose up --build
```

Or use the Makefile shortcuts:

```bash
make up        # docker compose up + tail logs until banner
make logs      # tail receiver logs
make down      # stop
make reset     # wipe volume + restart fresh
```

## Migrations & schema changes

The migrations in `db/migrations/` are mounted into Postgres's
`docker-entrypoint-initdb.d`. **They only run on first boot** — when the
data directory is empty. If you've started Strathon before and then add a
new migration file, you need either:

- `docker compose down -v` to wipe the volume and re-initialize (loses
  all data), or
- Apply the new migration manually:
  ```bash
  docker compose exec -T postgres psql -U strathon -d strathon \
      < db/migrations/00X_new.sql
  ```

This is a well-known Postgres-on-Docker pattern. A proper migration tool
(Alembic) lands in a future release.

## Production deployment

For real deployments, change at minimum:

1. **Rotate the seeded dev key.** Create a real key via
   `POST /v1/api_keys`, then revoke the dev key. See `docs/api_keys.md`.
2. **Put a reverse proxy in front.** The receiver speaks HTTP. Terminate
   TLS, restrict `/v1/api_keys/*` to admin access, add rate limiting.
3. **Override `POSTGRES_PASSWORD`.** The default `strathon_dev` is in
   the repo.
4. **Mount Postgres data on durable storage.** The default named volume
   `strathon_postgres_data` lives on the Docker host.

A production deploy recipe (Fly.io / Render / managed Postgres) ships in
a later release.

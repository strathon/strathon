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

1. Postgres pulls and initializes (empty database)
2. The receiver builds and starts
3. The receiver runs `alembic upgrade head` in its startup lifespan,
   creating the schema and seeding the dev API key. Idempotent on every
   subsequent start.
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
| `STRATHON_AUTO_MIGRATE`               | `true`           | Run `alembic upgrade head` at startup. |
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

Strathon uses [Alembic](https://alembic.sqlalchemy.org/) for schema
management. Migrations live in `receiver/alembic/versions/` and run
automatically when the receiver starts (idempotent — already-applied
migrations are a no-op).

When you add a new migration file and restart the receiver, the new
revision applies automatically. No manual psql commands, no wiping the
volume, no first-boot footgun.

To create a new revision:

```bash
cd receiver
DATABASE_URL=postgresql://... alembic revision -m "Add foo column"
# Edit the generated file in alembic/versions/
```

To inspect the current state:

```bash
cd receiver
DATABASE_URL=postgresql://... alembic current
DATABASE_URL=postgresql://... alembic history
```

To disable the receiver's auto-migrate behavior (e.g. if you run
migrations as a separate deploy step), set `STRATHON_AUTO_MIGRATE=false`
in your environment. The receiver will then assume migrations have
already been applied and start normally.

To apply migrations manually (with auto-migrate off, or for ops runbook
use):

```bash
docker compose exec receiver alembic upgrade head
```

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

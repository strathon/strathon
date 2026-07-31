# Self-hosting Strathon

Strathon ships as a docker-compose stack: Postgres, the receiver, and the
dashboard in three containers sharing one network. From a fresh clone to a
working receiver is typically under 60 seconds (most of which is the first
Postgres image pull).

## Prerequisites

- Docker 24+ with the Compose plugin (`docker compose`, not the deprecated
  `docker-compose`)
- 200 MB of disk for the Postgres volume
- Ports 4318 (receiver), 3000 (dashboard), and 5432 (Postgres) free

## Standing it up

```bash
git clone https://github.com/strathon/strathon.git
cd strathon
docker compose up
```

That is the entire local trial: no configuration needed. For anything
beyond a local trial, set the security keys first -- copy the template,
fill in the three keys (generation commands are inside the file), and
Compose picks the `.env` up automatically from the repo root:

```bash
cp .env.example .env
# edit .env: set STRATHON_AUDIT_HMAC_KEY, STRATHON_ENCRYPTION_KEY,
# STRATHON_PASSWORD_PEPPER (and optionally STRATHON_REQUIRE_SECURITY_KEYS=true
# to make missing keys a hard boot failure -- see Hardened mode below)
docker compose up -d
```

On first start:

1. Postgres pulls and initializes (empty database)
2. The receiver builds and starts
3. The receiver runs `alembic upgrade head` in its startup lifespan,
   creating the schema. Idempotent on every subsequent start.
4. If you set `STRATHON_SEED_DEV_KEY=true`, a well-known dev
   key is seeded and the receiver prints a quickstart banner with the key
   value, endpoint, and rotation hint. This is off by default (and never
   seeded in cloud mode) because the key value is publicly known; enable it
   only for local development. Without it, create a real key with
   `POST /v1/api_keys` after registering the first user.

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

### Bare-metal (Python + existing PostgreSQL)

If you already run PostgreSQL and prefer to run the receiver as a normal
Python process (systemd, supervisor, tmux — anything but Docker), install it
from source and point it at your database. The receiver ships from this repo,
not PyPI (the `strathon` package on PyPI is the SDK your agent imports, a
separate thing):

```bash
# 1. Create the database and role
createuser -P strathon
createdb -O strathon strathon

# 2. Install the receiver from the repo into a venv
git clone https://github.com/strathon/strathon.git
cd strathon/receiver
python -m venv .venv && . .venv/bin/activate
pip install .

# 3. Copy the env template and fill in values
cp .env.example .env
# then edit .env: set DATABASE_URL, STRATHON_AUDIT_HMAC_KEY,
# STRATHON_ENCRYPTION_KEY, STRATHON_PASSWORD_PEPPER
# (generation commands are in the file and in the Security keys section below)

# 4. Load the env vars into your shell (the receiver does NOT auto-load .env)
set -a; source .env; set +a

# 5. Run migrations, then start the receiver (run both from receiver/)
alembic upgrade head
uvicorn main:app --host 0.0.0.0 --port 4318
```

Under systemd, put the env vars in an `EnvironmentFile` instead of exporting
in a shell, and run from the `receiver/` directory:

```ini
[Service]
Type=simple
User=strathon
WorkingDirectory=/opt/strathon/receiver
EnvironmentFile=/etc/strathon/receiver.env
ExecStartPre=/opt/strathon/receiver/.venv/bin/alembic upgrade head
ExecStart=/opt/strathon/receiver/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 4318
Restart=on-failure
```

The dashboard is a separate Next.js app; build it once (`cd dashboard && npm
run build && npm run start`) or point a reverse proxy at both processes. It
reads two env vars of its own: `RECEIVER_URL` (where to reach the receiver;
default `http://localhost:4318`, so same-box setups need nothing) and
`STRATHON_COOKIE_SECURE` (set `true` only when the dashboard is served over
HTTPS -- over plain HTTP the browser drops Secure cookies and login fails).
`STRATHON_AUTO_MIGRATE=true` (default) lets the receiver run pending
migrations at boot so you can drop the `ExecStartPre` line if you prefer.

## Verifying

```bash
# Liveness probe (lightweight; "is the process up?")
curl http://localhost:4318/health

# Readiness probe (deep dependency check; "should traffic be routed here?")
curl http://localhost:4318/ready

# Authenticated request (needs STRATHON_SEED_DEV_KEY=true; otherwise use a key you created)
curl -H "Authorization: Bearer stra_dev_local_default_project_do_not_use_in_production" \
  http://localhost:4318/v1/policies

# Prometheus metrics
curl http://localhost:4318/metrics
```

A healthy `/ready` response looks like:

```json
{
  "status": "ready",
  "checks": {
    "db": {"status": "ok", "latency_ms": 1.21},
    "migrations": {"status": "ok", "current": "<head>", "head": "<head>"},
    "retention_task": {"status": "ok"},
    "retention_cleanup_task": {"status": "ok"},
    "webhook_sweeper_task": {"status": "ok"},
    "budget_monitor_task": {"status": "ok"},
    "audit_partition_task": {"status": "ok"},
    "spans_partition_task": {"status": "ok"}
  }
}
```

A failing check flips `status` to `"not_ready"`, the HTTP status to `503`,
and adds a short `reason` field to the failed check. See the
[health probes](#health-probes) section below for the Kubernetes wiring.

Or run one of the framework demos. They authenticate with the seeded dev key,
so run them with `STRATHON_SEED_DEV_KEY=true`, or set your own key in the script:

```bash
pip install strathon langchain cel-python
python examples/intervention_demo.py
```

## Configuration

All knobs are env vars. Copy `.env.example` to `.env` to override
defaults; the compose file picks it up automatically.

| Variable                              | Default          | Purpose                                |
|---------------------------------------|------------------|----------------------------------------|
| `POSTGRES_PASSWORD`                            | `strathon_dev`   | Postgres password.                                                                              |
| `STRATHON_LOG_LEVEL`                           | `INFO`           | Receiver log verbosity.                                                                         |
| `STRATHON_LOG_FORMAT`                          | `text`           | `text` or `json` (one record per line).                                                         |
| `STRATHON_AUTO_MIGRATE`                        | `true`           | Run `alembic upgrade head` at startup.                                                          |
| `STRATHON_SAMPLING_RATE`                       | `1.0`            | 0.0-1.0. See docs/sampling.md.                                                                  |
| `STRATHON_RETENTION_ENABLED`                   | `true`           | Background trace cleanup.                                                                       |
| `STRATHON_RETENTION_INTERVAL_SECONDS`          | `3600`           | Seconds between retention sweeps.                                                               |
| `STRATHON_RETENTION_BATCH_SIZE`                | `5000`           | Max traces deleted per project/sweep.                                                           |
| `STRATHON_RATE_LIMIT_ENABLED`                  | `true`           | Per-key in-memory rate limiter. Set `false` to bypass entirely.                                 |
| `STRATHON_RATE_LIMIT_REQUESTS_PER_SECOND`      | `100`            | Sustained per-key throughput. Token bucket refills at this rate.                                |
| `STRATHON_RATE_LIMIT_BURST`                    | `200`            | Token-bucket capacity. Maximum momentary burst before throttling.                               |
| `STRATHON_PUBLIC_URL`                          | `http://localhost:4318` | Public base URL used to build links in outbound notifications (approval approve/deny). Set to your reverse-proxy address or the links point to localhost. |
| `STRATHON_WEBHOOK_REDIS_URL`                   | `` (empty)       | Redis broker for async webhook/alert delivery. Empty uses an in-memory broker (inline send, fine for dev). Set to e.g. `redis://localhost:6379/0` for durable, retried delivery in production. |

### Security keys

Three secrets harden a production deployment. Each is an environment variable:
generate a value once, put it in your `.env` (or your secrets manager), and the
receiver reads it at boot. The values are never written to a file or the
database, so the only copy is the one you set. Keep each value stable for the
life of the deployment unless you are deliberately rotating it.

Password salts are separate and need no configuration: Argon2id generates a
unique random salt for every password automatically and stores it alongside
the hash, so there is nothing to set.

Generate each one (the commands differ; the encryption key is not a plain hex
string):

```bash
# STRATHON_AUDIT_HMAC_KEY  (64-char hex)
python -c 'import secrets; print(secrets.token_hex(32))'

# STRATHON_ENCRYPTION_KEY  (Fernet key, base64; generated differently)
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'

# STRATHON_PASSWORD_PEPPER (64-char hex)
python -c 'import secrets; print(secrets.token_hex(32))'
```

Then add them to `.env`:

```
STRATHON_AUDIT_HMAC_KEY=<paste the hex value>
STRATHON_ENCRYPTION_KEY=<paste the Fernet value>
STRATHON_PASSWORD_PEPPER=<paste the hex value>
```

| Variable                     | Required?            | Purpose                                                                 |
|------------------------------|----------------------|-------------------------------------------------------------------------|
| `STRATHON_AUDIT_HMAC_KEY`    | Yes for production   | Signs the tamper-evident audit hash chain. Empty in self-hosted mode falls back to a dev key with a warning; empty in cloud mode it raises rather than sign with a known value. At least 32 bytes. |
| `STRATHON_ENCRYPTION_KEY`    | Recommended          | Encrypts stored TOTP secrets at rest. Without it, TOTP secrets are stored unencrypted. Must be a valid Fernet key. |
| `STRATHON_PASSWORD_PEPPER`   | Recommended          | Extra secret mixed into password hashing for defense in depth.          |

Changing a value after first use has consequences, so treat them as fixed:
a new `STRATHON_PASSWORD_PEPPER` invalidates every existing password, and a new
`STRATHON_ENCRYPTION_KEY` makes already-encrypted TOTP secrets unreadable.

Rotating `STRATHON_AUDIT_HMAC_KEY`: treat it as fixed. Verification always
recomputes with the *current* key, so changing it makes every existing row
fail verification -- the chain reads as broken from the rotation point back.
Each row stores an `hmac_key_id` (always `1` in this release) as groundwork
for real rotation in a future release, where the id increments and previous
keys stay available for verifying old rows. Until that ships, a key change is
a chain reset, not a rotation. Full detail is in [docs/audit.md](audit.md).

### Hardened mode (require keys at boot)

By default the receiver boots with zero configuration for local trials:
missing security keys fall back to development defaults with loud startup
warnings. If you would rather have a missing key be a hard failure -- the
receiver refuses to start instead of warning -- set:

```bash
STRATHON_REQUIRE_SECURITY_KEYS=true
```

Nothing else changes: same features, same single-tenant behavior. The
receiver simply refuses to start until all three keys are set, and the
startup error names exactly which ones are missing. Recommended for any
internet-facing deployment. (Cloud mode always enforces this.)

For internet-facing instances, registration is defended in layers:
per-IP rate limiting (shares the login limiter), a global cap on total
registrations per minute (`STRATHON_REGISTER_GLOBAL_LIMIT_PER_MINUTE`,
default 30) as a backstop against distributed floods, account lockout on
repeated failed logins, and `STRATHON_REGISTRATION_ENABLED=false` to
close registration entirely once your team is onboarded -- the strongest
control. Client IPs are taken from the socket, never from
`X-Forwarded-For`, unless you explicitly opt in behind a trusted reverse
proxy (`STRATHON_RATE_LIMIT_TRUST_FORWARDED_FOR=true`).

### Browser security headers

The dashboard sends a strict Content-Security-Policy on every response: a
per-request nonce plus `strict-dynamic`, so an injected `<script>` has no
valid nonce and the browser refuses to run it. Nothing to configure.

Two headers are sent only when the request arrives over TLS --
`Strict-Transport-Security` and `upgrade-insecure-requests`. Behind a reverse
proxy the connection to the dashboard is plain HTTP, so the dashboard reads
the standard `X-Forwarded-Proto: https` header your proxy should already set
(nginx: `proxy_set_header X-Forwarded-Proto $scheme;`, Caddy and Traefik do it
by default). If that header is missing, everything still works -- you simply
do not get HSTS. They are deliberately withheld on plain HTTP:
`upgrade-insecure-requests` would rewrite same-origin asset URLs to `https://`
and leave you with a blank dashboard, and HSTS would pin the host to HTTPS for
a year, which is a hard state to undo on an internal deployment.

### If you lose a key

Each key fails independently, and none of them brick the deployment:

- **`STRATHON_PASSWORD_PEPPER` lost**: every existing password stops
  verifying (logins fail with the normal "Invalid email or password"; there
  is no special error, by design). TOTP and backup codes are unaffected.
  Recover by setting a new pepper, then resetting each affected account:
  `python -m admin_cli reset-password --email <email>` (new hashes use the
  pepper currently in the environment).
- **`STRATHON_ENCRYPTION_KEY` lost or changed**: stored TOTP secrets can no
  longer be decrypted, so authenticator codes are rejected ("Invalid MFA
  code") and the receiver logs the cause. **Backup codes keep working** --
  they are stored as hashes and never needed the key -- so users can log in
  with a backup code and re-enroll MFA. For a user without their backup
  codes: `python -m admin_cli reset-password --email <email> --disable-mfa`.
- **`STRATHON_AUDIT_HMAC_KEY` lost or changed**: existing audit rows fail
  verification (chain reset, as above). Nothing else is affected; new rows
  chain normally under the new key.

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

## Account recovery (locked-out owner)

If the sole owner loses their password and second factor (and no SMTP is
configured for the email reset flow), use the offline recovery CLI. It runs
directly against the database, so it needs `DATABASE_URL` and host access but
not a running receiver:

```bash
cd receiver
DATABASE_URL=postgresql://localhost/strathon \
  python -m admin_cli reset-password --email owner@example.com

# If the owner also lost their TOTP device / recovery codes:
DATABASE_URL=postgresql://localhost/strathon \
  python -m admin_cli reset-password --email owner@example.com --disable-mfa
```

It prints a one-time temporary password; the user must change it on next
login. (`strathon-admin reset-password ...` is the same command if the
receiver package is pip-installed.) Because it requires direct database
access, only an operator who already controls the host can run it; it
grants no privilege beyond what raw database access already implies.

## Migrations & schema changes

Strathon uses [Alembic](https://alembic.sqlalchemy.org/) for schema
management. Migrations live in `receiver/alembic/versions/` and run
automatically when the receiver starts (idempotent: already-applied
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

### HTTPS

The receiver speaks plain HTTP. For production, terminate TLS with a
reverse proxy. Two options:

**Caddy** (automatic HTTPS, recommended for simplicity):

```
strathon.yourdomain.com {
    reverse_proxy localhost:4318
}
```

Save as `Caddyfile`, run `caddy run`. Caddy obtains and renews
certificates from Let's Encrypt automatically. No further config needed.

**nginx** (if you already run nginx):

```nginx
server {
    listen 443 ssl;
    server_name strathon.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/strathon.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/strathon.yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:4318;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Obtain certificates with `certbot --nginx` or your preferred ACME client.

After configuring HTTPS, update your SDK endpoint:

```python
client = Client(
    api_key="stra_...",
    endpoint="https://strathon.yourdomain.com",
)
```

### Health probes

The receiver exposes two probe endpoints with distinct semantics, matching
the Kubernetes liveness/readiness convention:

- **`/health`**: Liveness probe. Returns `200 {"status": "ok", ...}` as
  long as the event loop is responsive. Does not touch the database or
  any background task. Use this when you want "restart the pod if the
  process is wedged."
- **`/ready`**: Readiness probe. Returns `200` with a per-check
  breakdown when every dependency is healthy, `503` with the same
  shape when any check fails. Checks: database connectivity, schema
  migration version (compared to the code's expected head), and the
  background tasks (retention sweep and cleanup, webhook sweeper,
  budget monitor, audit and spans partition maintenance). Use this
  when you want "stop routing traffic to this replica until it
  recovers."

Keeping liveness lightweight matters: a deep check on the liveness
endpoint would cause Kubernetes to kill an otherwise-healthy pod the
moment a downstream dependency hiccups, replacing a routing problem
with an availability problem.

Both endpoints are unauthenticated by design: Prometheus scrapers and
Kubernetes probes commonly run without credentials. Restrict them at
the network layer (ACL or reverse proxy) if your environment requires
it.

Example Kubernetes pod spec:

```yaml
spec:
  containers:
  - name: receiver
    image: ghcr.io/strathon/receiver:latest
    ports:
    - containerPort: 4318

    livenessProbe:
      httpGet:
        path: /health
        port: 4318
      periodSeconds: 10
      failureThreshold: 3
      timeoutSeconds: 1

    readinessProbe:
      httpGet:
        path: /ready
        port: 4318
      periodSeconds: 5
      failureThreshold: 3
      timeoutSeconds: 2
```

The receiver's readiness checks are individually bounded under 500ms,
so a 2-second probe timeout has comfortable headroom even when the
database is briefly slow.

### Rate limiting

The receiver enforces a per-identifier token-bucket rate limit by
default (100 req/s sustained, 200 burst). The identifier is the
`Authorization` header's SHA-256 digest for authenticated requests,
the client IP otherwise (`X-Forwarded-For` leftmost when present).
`/health`, `/ready`, and `/metrics` are exempt: probes always answer
regardless of load.

Responses include `X-RateLimit-Limit` and `X-RateLimit-Remaining`
headers so well-behaved clients can self-throttle. On rejection the
response is `429 Too Many Requests` with `Retry-After` (seconds, RFC
9110) and a JSON body `{"detail": "rate limit exceeded, retry in Ns"}`.

Tune via the three `STRATHON_RATE_LIMIT_*` env vars listed above.
Set `STRATHON_RATE_LIMIT_ENABLED=false` to bypass entirely; do this
when running behind a reverse proxy that already enforces limits.

**Multi-replica caveat.** State is per-process: in an N-replica deploy
each replica holds its own buckets, so the effective per-key ceiling
is `N × STRATHON_RATE_LIMIT_REQUESTS_PER_SECOND`. The dominant
self-hosting pattern is one receiver replica behind a load balancer,
which is unaffected. Multi-replica operators who need exact shared
limits should run a rate-limiting reverse proxy (nginx `limit_req`,
HAProxy `stick-table`, AWS WAF, Cloudflare, etc.) in front of the
receiver and set `STRATHON_RATE_LIMIT_ENABLED=false` to avoid
double-counting.

### Connection pooling caveat

If you put PgBouncer (or another connection pooler) between the receiver
and Postgres, **run it in session pooling mode**, not transaction pooling.

The budget monitor uses session-scoped Postgres advisory locks
(`pg_try_advisory_lock`) to ensure only one replica evaluates budgets
on each tick. In transaction-pooling mode, PgBouncer recycles
connections between transactions, which silently releases advisory
locks held by the monitor. The symptom is duplicate halts written by
multiple replicas racing on the same budget.

Either set `pool_mode = session` for the receiver's pool, point the
receiver at Postgres directly, or run with a single receiver replica
(advisory locks are still useful there as a guard against startup races).
The same caveat applies to anything else in the codebase that uses
session-scoped state on a Postgres connection.

### Backup and restore

All durable state lives in Postgres -- policies, spans, the audit chain, users,
and keys. Backing up the database backs up everything you need to recover; the
receiver and dashboard containers are stateless and are recreated from the
image. If you run Redis for webhook delivery (STRATHON_WEBHOOK_REDIS_URL), it
holds only the in-flight dispatch queue, not durable state: Postgres is the
source of truth for delivery status, and the sweeper re-enqueues any pending
delivery whose queued message was lost, so Redis does not need backing up. A
backup is a standard `pg_dump`.

```bash
# Docker Compose: dump from the postgres service
docker compose exec -T postgres \
  pg_dump -U strathon -d strathon --format=custom --file=/tmp/strathon.dump
docker compose cp postgres:/tmp/strathon.dump ./strathon-$(date +%F).dump

# Bare-metal: dump from your Postgres host
pg_dump -U strathon -d strathon --format=custom --file=strathon-$(date +%F).dump
```

The custom format (`--format=custom`) restores with `pg_restore` and supports
parallel restore. For a scheduled backup, run the same command from cron or a
sidecar and ship the file off the box; the encryption key and the other
secrets are in your `.env` (or secrets manager), not in the dump, so store
both together or the restored database cannot decrypt TOTP secrets.

Restore into an empty database:

```bash
# Docker Compose: stop the app, keep Postgres running, restore, restart
docker compose stop receiver dashboard
docker compose cp ./strathon-2026-01-01.dump postgres:/tmp/restore.dump
docker compose exec -T postgres \
  pg_restore -U strathon -d strathon --clean --if-exists /tmp/restore.dump
docker compose start receiver dashboard

# Bare-metal
pg_restore -U strathon -d strathon --clean --if-exists strathon-2026-01-01.dump
```

Two things specific to Strathon:

- **Match the schema version.** Restore into a receiver running the same or a
  newer release than the dump was taken from. A newer receiver runs any pending
  migrations on start (`STRATHON_AUTO_MIGRATE=true`); an older receiver against
  a newer dump is unsupported. Check the dump's version before a cross-release
  restore.
- **The audit hash chain stays intact** across a full-database dump and restore,
  because the dump captures every row including the chain links and anchors.
  Restoring a partial or row-filtered subset of `audit.events` breaks the
  chain's continuity and the tamper-evidence verification will report a gap --
  back up and restore the whole database, not selected tables.

For point-in-time recovery beyond nightly dumps, use Postgres WAL archiving or a
managed Postgres with continuous backup; nothing in Strathon precludes it, and
the partitioned `spans` tables restore like any other.

## Related

- [Getting started](getting-started.md): from running stack to first blocked call
- [Scaling guide](scaling.md): beyond a single node
- [Metrics](metrics.md): monitoring the receiver in production

# Strathon — AI Coding Assistant Context

## Project Structure
- `receiver/` — FastAPI backend (Python 3.12+, PostgreSQL 16)
- `sdk/` — Python SDK published as `strathon` on PyPI (Python 3.11+)
- `cli/` — CLI published as `strathon-cli` on PyPI (Python 3.11+)
- `dashboard/` — Next.js 16 operator UI (Node 24; see `dashboard/.nvmrc`)
- `tests/` — end-to-end integration tests
- `docs/` — technical documentation
- `benchmarks/` — load testing

## Key Technologies
- **Receiver:** FastAPI, SQLAlchemy 2.0 (async), Alembic, celpy (CEL), Argon2id, dramatiq
- **SDK:** OpenTelemetry, httpx, pydantic
- **Dashboard:** Next.js 16, React 19, TypeScript, Tailwind CSS
- **Database:** PostgreSQL 16 (partitioned spans, JSONB attributes)

## Development Commands
```bash
cd receiver && pip install -e ".[dev]" && python -m pytest tests/ -q
cd sdk && pip install -e ".[dev]" && python -m pytest tests/ -q
cd dashboard && npm ci && npm run dev   # see the lockfile note below
docker compose up
```

`npm ci` installs exactly what `package-lock.json` records and never writes it,
so it is right for running, building, and testing. Changing a dependency is the
one case for `npm install`: it resolves the ranges in `package.json` and is the
only command that rewrites the lockfile. Commit the result. If the two ever
disagree, `npm ci` fails with `EUSAGE` rather than installing something else.

Checks CI runs that are easy to miss locally:

```bash
ruff check .                            # rules are selected by name in ruff.toml
python scripts/check_cel_attributes.py  # every CEL attr in docs must be emitted
python scripts/check_doc_versions.py    # doc versions must match the pyprojects
python scripts/check_secrets.py --all   # also the pre-commit hook
```

## Conventions
- Commit messages: feat:, fix:, test:, docs:, perf:, chore:
- Raw SQL via text() calls for complex operations
- Pydantic models with extra="forbid"
- ruff + mypy clean on every commit
- Tests require live PostgreSQL (skip gracefully when unavailable)

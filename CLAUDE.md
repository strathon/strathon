# Strathon — AI Coding Assistant Context

## Project Structure
- `receiver/` — FastAPI backend (Python 3.12+, PostgreSQL 16)
- `sdk/` — Python SDK published as `strathon` on PyPI
- `cli/` — CLI published as `strathon-cli` on PyPI
- `dashboard/` — Next.js 16 operator UI
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
cd dashboard && npm install && npm run dev
docker compose up
```

## Conventions
- Commit messages: feat:, fix:, test:, docs:, perf:, chore:
- Raw SQL via text() calls for complex operations
- Pydantic models with extra="forbid"
- ruff + mypy clean on every commit
- Tests require live PostgreSQL (skip gracefully when unavailable)

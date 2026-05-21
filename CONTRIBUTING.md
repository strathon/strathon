# Contributing to Strathon

Thank you for contributing to Strathon. This guide covers setup,
standards, and the PR process.

## Development Setup

**Prerequisites:** Python 3.12+, PostgreSQL 16+, Docker + Compose

```bash
git clone https://github.com/strathon/strathon.git
cd strathon
docker compose up -d postgres

# Receiver
cd receiver
pip install -e ".[dev]"
alembic upgrade head
python -m pytest tests/ -q

# SDK
cd ../sdk
pip install -e ".[dev]"
python -m pytest tests/ -q

# CLI
cd ../cli
pip install -e .
strathon --version
```

### Key environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | Postgres connection string |
| `STRATHON_AUDIT_HMAC_KEY` | Yes | HMAC key for audit chain |
| `STRATHON_ENCRYPTION_KEY` | No | Fernet key for column encryption |
| `STRATHON_DOCS_ENABLED` | No | Enable /docs (default: false) |

## Code Standards

**Linter:** ruff (`ruff check .` before every commit)

**Security rules (non-negotiable):**
- No secrets in code — env vars only
- No competitor product names in code, comments, or commits
- `hmac.compare_digest` for all secret comparisons, never `==`
- `extra="forbid"` on all Pydantic request models
- Parameterized SQL only — never f-strings with user input
- google-re2 for any user-facing regex

**Commit messages:** conventional commits format.

```
feat(policies): add batch disable endpoint
fix(auth): timing-safe comparison on session lookup
docs: add CEL expression guide
```

## Testing

```bash
cd receiver && python -m pytest tests/ -q   # Receiver (needs Postgres)
cd sdk && python -m pytest tests/ -q         # SDK
python -m pytest tests/test_file.py -v       # Single file
```

- Every endpoint: at least 3 tests (happy path, error, auth check)
- Clean up test data after each test
- Mock external services (Slack, Discord, GitHub)
- Use `DEV_KEY` for authenticated test requests

## Pull Request Process

1. Fork → feature branch → make changes + tests
2. `ruff check .` clean + all tests pass
3. Conventional commit message
4. Open PR against `main`, complete the PR template checklist
5. CI must pass before review

## Architecture

```
strathon/
├── receiver/       # FastAPI API + Postgres (all business logic)
├── sdk/            # Python SDK (10 framework instrumentations)
├── cli/            # CLI tool (Click, 12 command groups)
├── examples/       # Example scripts for all frameworks
├── docs/           # Documentation
└── docker-compose.yml
```

**Key rule:** only the receiver talks to Postgres. SDK, CLI, and
dashboard are API clients.

## Questions?

Open a GitHub issue or discussion. Security issues: security@getstrathon.com

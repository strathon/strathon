# Integration tests

Cross-cutting tests that exercise the SDK and receiver together. Each test
boots a real receiver subprocess pointing at a real Postgres and drives
real framework code through it.

## Running

Prerequisites:

- Postgres reachable at `DATABASE_URL` (default
  `postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon`)
- SDK + receiver dependencies installed (`pip install -e sdk/ -e receiver/`)
- `psycopg` for direct DB assertions: `pip install psycopg`

From the repo root:

```bash
pytest tests/
```

Tests skip cleanly if Postgres isn't reachable, so contributors without
infrastructure can still run the rest of `pytest`.

## What's here

- `test_cross_framework_parity.py` — the central parity claim. One CEL
  policy, three frameworks (LangGraph, CrewAI, OpenAI Agents SDK), three
  identical assertions per framework: block fires, exception carries the
  right policy name, audit row in `policy_matches`, span in `spans` with
  the correct `strathon.framework` attribute.

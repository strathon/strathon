"""Repositories — session-aware DB operations.

Each repository module owns one domain area's persistence (auth, policies,
retention, traces). Repositories never construct sessions; they receive an
`AsyncSession` as their first argument. Endpoints get sessions via
FastAPI's `Depends(get_db_session)`; background tasks construct sessions
directly via `async_session_maker()`.

Repository functions:
- Take `session: AsyncSession` as the first arg
- Return Pydantic schemas (or scalars), never raw ORM objects after the
  function returns — keeps session lifecycle out of the caller's concern
- Don't catch exceptions; let SQLAlchemy errors propagate to the endpoint
  layer where HTTPException translation happens
- Don't commit explicitly when used inside `get_db_session` (that
  generator commits/rollbacks for the whole request); DO commit when
  called from a background task that constructs its own session
"""

"""HTTP routers for the Strathon receiver.

Every FastAPI endpoint lives in one of the sibling modules
here (health, traces, policies, api_keys, intervention). main.py owns
app construction + lifespan only.

Each router exports a module-level `router = APIRouter(...)` that main.py
mounts via app.include_router(). Shared dependencies (authentication,
project resolution) live in _deps.py.
"""

# Receiver image: FastAPI app served by uvicorn. Built and published as
# ghcr.io/strathon/receiver. Runs alongside the dashboard image and Postgres
# via docker compose.

FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/strathon/strathon" \
      org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast Python deps
RUN pip install --no-cache-dir uv

COPY pyproject.toml ./
COPY requirements.lock ./
# Install all dependencies from a fully-pinned, hash-locked requirements file
# so the image is reproducible and a substituted or tampered dependency fails
# the hash check (supply-chain hardening). This runs before the source is copied
# so the (large, slow) dependency layer stays cached across code changes.
# Regenerate after any dependency change with:
#   uv pip compile pyproject.toml --generate-hashes -o requirements.lock
RUN uv pip install --system --no-cache --require-hashes -r requirements.lock

COPY . .

# Install the receiver package itself without re-resolving dependencies -- they
# are already pinned and installed from the lock above; letting this re-resolve
# would defeat the lock.
RUN uv pip install --system --no-cache --no-deps .

# Run as a non-root user, matching the dashboard image. A root-running
# enforcement service is an unnecessary blast-radius risk; the app needs no
# privileged ports (4318 is unprivileged) or root-owned paths at runtime.
RUN addgroup --system --gid 1001 strathon \
    && adduser --system --uid 1001 --ingroup strathon strathon \
    && chown -R strathon:strathon /app
USER strathon

EXPOSE 4318

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "4318"]

# Strathon - single image with receiver (FastAPI) + dashboard (Next.js)
# Both processes managed by supervisord.

# ============================================================
# Stage 1: Build the Next.js dashboard
# ============================================================
FROM node:22-alpine AS dashboard-builder

WORKDIR /build/dashboard

# Install pnpm for faster, more efficient dependency management
RUN npm install -g pnpm@9

# Copy dependency manifests first for better cache utilization
COPY dashboard/package.json dashboard/pnpm-lock.yaml* ./
RUN pnpm install --frozen-lockfile || pnpm install

# Copy source and build
COPY dashboard/ ./
RUN pnpm build

# ============================================================
# Stage 2: Install Python receiver dependencies
# ============================================================
FROM python:3.12-slim AS receiver-builder

WORKDIR /build/receiver

# Install uv for fast Python package management
RUN pip install --no-cache-dir uv

# Copy and install dependencies into a virtualenv we can copy later
COPY receiver/pyproject.toml ./
RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python --no-cache .

# ============================================================
# Stage 3: Final runtime image
# ============================================================
FROM python:3.12-slim

WORKDIR /app

# Install Node.js runtime (for Next.js) and supervisord
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    npm \
    supervisor \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g pnpm@9

# Copy Python virtualenv from builder
COPY --from=receiver-builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy receiver source
COPY receiver/ /app/receiver/

# Copy dashboard build output
COPY --from=dashboard-builder /build/dashboard/.next /app/dashboard/.next
COPY --from=dashboard-builder /build/dashboard/public /app/dashboard/public
COPY --from=dashboard-builder /build/dashboard/node_modules /app/dashboard/node_modules
COPY --from=dashboard-builder /build/dashboard/package.json /app/dashboard/

# Copy supervisord config
COPY docker/supervisord.conf /etc/supervisor/conf.d/strathon.conf

# Create non-root user for runtime
RUN useradd -r -u 1000 -m strathon \
    && chown -R strathon:strathon /app

EXPOSE 3000 4318

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:4318/health && curl -f http://localhost:3000 || exit 1

CMD ["supervisord", "-c", "/etc/supervisor/conf.d/strathon.conf", "-n"]

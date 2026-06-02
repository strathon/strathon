# Receiver image: FastAPI app served by uvicorn. Built and published as
# ghcr.io/strathon/receiver. Runs alongside the dashboard image and Postgres
# via docker compose.

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast Python deps
RUN pip install --no-cache-dir uv

COPY pyproject.toml ./
RUN uv pip install --system --no-cache .

COPY . .

EXPOSE 4318

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "4318"]

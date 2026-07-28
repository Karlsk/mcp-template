# Multi-stage build: install deps with uv, run on a slim Python image.
# Build context is the repo root (see deploy/docker-compose.yml).

# ---- Builder: resolve & install dependencies with uv ----
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Install third-party dependencies first (cached unless pyproject/lock change).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Install the project itself (app package).
COPY app ./app
RUN uv sync --frozen --no-dev

# ---- Runtime: slim image with only the venv + config ----
FROM python:3.13-slim-bookworm AS runtime

WORKDIR /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    SDN_CONFIG_FILE=/app/config/sdn_controller.yaml

COPY --from=builder /app/.venv /app/.venv
COPY config /app/config

EXPOSE 8000

# Bind to 0.0.0.0 so the server is reachable outside the container.
CMD ["sdn-mcp", "--host", "0.0.0.0", "--port", "8000"]

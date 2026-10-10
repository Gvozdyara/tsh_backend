# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# TSH knowledge base - FastAPI backend.
#
# Base image: the official uv image, so the dependencies are installed by uv
# from the frozen uv.lock (reproducible, no pip resolver at build time) on an
# Alpine musl userland.
#
# The API listens on 0.0.0.0 inside the container: the loopback binding belongs
# to the orchestrator (compose publishes 127.0.0.1:<random>), never to the
# image.
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-alpine AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    # container port; it is read by the CMD below, so compose (or `docker run
    # -e TSH_BACKEND_PORT=…`) can move the API without touching the image
    TSH_BACKEND_PORT=37296 \
    TSH_LOG_LEVEL=info

WORKDIR /app

# curl is used by the container healthcheck and the compose healthcheck
RUN apk add --no-cache curl

# 1 - dependency layer, rebuilt only when the locked dependency set changes
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 2 - application layer
COPY app ./app
# maintenance scripts (uv run python -m scripts.import_json …)
COPY scripts ./scripts
# The schema also lives here: the backend applies it idempotently on start and
# backend/schema/01-schema.sql (the same file) is bind mounted into the postgres
# image for the very first creation of the data volume.
COPY schema/01-schema.sql /app/schema/01-schema.sql

# Non root runtime user.
RUN adduser -D -u 10001 -h /home/tsh tsh && chown -R tsh:tsh /app
USER tsh

EXPOSE 37296

HEALTHCHECK --interval=20s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS "http://127.0.0.1:${TSH_BACKEND_PORT}/api/health" || exit 1

# `sh -c exec` keeps the port in the environment (compose / .env is the single
# source of truth) while uvicorn still replaces the shell and receives SIGTERM
# as PID 1.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${TSH_BACKEND_PORT}\" --proxy-headers --forwarded-allow-ips '*' --log-level \"${TSH_LOG_LEVEL}\" --no-access-log"]

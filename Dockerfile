# syntax=docker/dockerfile:1.7

# ---- Stage 1: build a venv from the script's PEP 723 inline metadata ----
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /build
COPY server.py a2a_bridge.py ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv \
 && uv export --script server.py --no-hashes --format requirements-txt -o /tmp/requirements.txt \
 && uv export --script a2a_bridge.py --no-hashes --format requirements-txt -o /tmp/a2a-requirements.txt \
 && VIRTUAL_ENV=/opt/venv uv pip install -r /tmp/requirements.txt -r /tmp/a2a-requirements.txt

# ---- Stage 2: minimal runtime ----
FROM python:3.13-slim AS runtime

# BRIDGE_VERSION is set by the release workflow from the git tag (e.g.
# v0.1.4); falls back to "dev" for local docker builds. Surfaced via
# /api/status and shown in the web UI header.
ARG BRIDGE_VERSION=dev

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=8765 \
    BRIDGE_DATA_DIR=/data \
    BRIDGE_VERSION=${BRIDGE_VERSION} \
    BRIDGE_MODE=mcp

RUN groupadd --system --gid 1000 app \
 && useradd --system --uid 1000 --gid app --home-dir /app --shell /usr/sbin/nologin app \
 && mkdir -p /app /data \
 && chown -R app:app /app /data

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app server.py /app/server.py
COPY --chown=app:app a2a_bridge.py /app/a2a_bridge.py
COPY --chown=app:app docker-entrypoint.sh /app/docker-entrypoint.sh

USER app
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8765

ENTRYPOINT ["/app/docker-entrypoint.sh"]

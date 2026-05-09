# syntax=docker/dockerfile:1.7

# ---- Stage 1: build a venv from the script's PEP 723 inline metadata ----
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /build
COPY server.py ./server.py

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv \
 && uv export --script server.py --no-hashes --format requirements-txt -o /tmp/requirements.txt \
 && VIRTUAL_ENV=/opt/venv uv pip install -r /tmp/requirements.txt

# ---- Stage 2: minimal runtime ----
FROM python:3.13-slim AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=8765 \
    BRIDGE_DATA_DIR=/data

RUN groupadd --system --gid 1000 app \
 && useradd --system --uid 1000 --gid app --home-dir /app --shell /usr/sbin/nologin app \
 && mkdir -p /app /data \
 && chown -R app:app /app /data

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app server.py /app/server.py

USER app
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8765

CMD ["python", "/app/server.py"]

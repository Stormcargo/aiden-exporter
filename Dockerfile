# ── build stage: install deps with UV ────────────────────────────────────────
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Install deps into /app/.venv without the project itself first (layer cache)
COPY pyproject.toml .
RUN uv sync --no-install-project --no-dev

# Now copy source and do a full sync
COPY exporter.py .
RUN uv sync --no-dev

# ── runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

# Non-root user
RUN useradd --no-create-home --uid 1000 exporter
USER exporter

# Copy the venv and source from the build stage
COPY --from=builder --chown=exporter:exporter /app/.venv /app/.venv
COPY --from=builder --chown=exporter:exporter /app/exporter.py /app/exporter.py

ARG VERSION=dev
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_VERSION=${VERSION}

EXPOSE 9090

ENTRYPOINT ["python", "/app/exporter.py"]

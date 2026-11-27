FROM node:22.22.3-bookworm-slim AS frontend
WORKDIR /app/frontend
ENV NODE_OPTIONS=--max-old-space-size=768 \
    UV_THREADPOOL_SIZE=2
COPY frontend/package.json frontend/package-lock.json ./
RUN nice -n 19 ionice -c3 npm ci --no-audit --no-fund --prefer-offline
COPY frontend/ ./
RUN nice -n 19 ionice -c3 npm run build

FROM ghcr.io/astral-sh/uv:0.11.19 AS uv

FROM python:3.12.13-slim AS python-build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_CONCURRENT_DOWNLOADS=2 \
    UV_CONCURRENT_BUILDS=1 \
    UV_CONCURRENT_INSTALLS=2 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
COPY --from=frontend /app/frontend/dist /app/static
COPY --from=uv /uv /usr/local/bin/uv
COPY .python-version pyproject.toml uv.lock ./
COPY src/ src/
RUN nice -n 19 ionice -c3 uv sync --locked --no-dev --no-editable \
    && nice -n 19 ionice -c3 rm -rf /root/.cache/uv

FROM python:3.12.13-slim
RUN nice -n 19 ionice -c3 apt-get update \
    && nice -n 19 ionice -c3 apt-get install -y --no-install-recommends netcat-openbsd openssh-client \
    && rm -rf /var/lib/apt/lists/*
ARG GPU_REVISION=unknown
LABEL org.opencontainers.image.title="GPU 实验管理台" org.opencontainers.image.version="0.30.3" \
      org.opencontainers.image.revision=${GPU_REVISION}
WORKDIR /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GPU_DATA_DIR=/app/data \
    GPU_SECRETS_DIR=/app/secrets \
    GPU_STATIC_DIR=/app/static
COPY --from=python-build /app/.venv /app/.venv
COPY --from=python-build /app/static /app/static
RUN groupadd --gid 10001 gpu-console \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin gpu-console
USER 10001:10001
EXPOSE 8080
CMD ["gpu-admin", "web"]

# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.11.17 AS uv

FROM node:22-bookworm-slim AS node-deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts

FROM python:3.12.11-slim-bookworm

ARG SOURCE_SHA=development
ARG VERSION=0.1.0
ARG SCHEMA_REVISION=development

LABEL org.opencontainers.image.source="https://github.com/Nova3322/TradeOps" \
      org.opencontainers.image.revision="${SOURCE_SHA}" \
      org.opencontainers.image.version="${VERSION}" \
      io.tradeops.source-sha="${SOURCE_SHA}" \
      io.tradeops.schema-revision="${SCHEMA_REVISION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_HTTP_TIMEOUT=300 \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_LINK_MODE=copy

RUN groupadd --system tradingops \
    && useradd --system --gid tradingops --home-dir /app --shell /usr/sbin/nologin tradingops

WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY --from=node-deps /usr/local/bin/node /usr/local/bin/node
COPY --from=node-deps /app/node_modules ./node_modules
COPY pyproject.toml uv.lock README.md ./
COPY package.json package-lock.json ./
COPY docs ./docs
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY src ./src
RUN uv sync --frozen --no-dev --no-cache \
    && chown -R tradingops:tradingops /app

USER tradingops
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "trading-api"]

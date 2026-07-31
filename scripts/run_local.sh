#!/usr/bin/env bash
set -euo pipefail

docker compose up -d postgres

for _ in {1..30}; do
  if docker compose exec -T postgres pg_isready -U trading -d trading_local >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

docker compose exec -T postgres pg_isready -U trading -d trading_local >/dev/null
uv run python scripts/setup_local.py
exec uv run trading-api

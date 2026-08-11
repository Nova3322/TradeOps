#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${TRADING_CONFIG_DIR:-}" && ! -f .env.local && ! -f .env.production.local ]]; then
  trading_git_common_dir="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  if [[ -n "$trading_git_common_dir" ]]; then
    trading_shared_checkout="$(dirname "$trading_git_common_dir")"
    if [[ -f "$trading_shared_checkout/.env.local" || -f "$trading_shared_checkout/.env.production.local" ]]; then
      export TRADING_CONFIG_DIR="$trading_shared_checkout"
    fi
  fi
fi

local_secret_dir="$PWD/.local/runtime-secrets"
mkdir -p "$local_secret_dir"
chmod 700 "$PWD/.local" "$local_secret_dir"

ensure_local_secret() {
  local output_file=$1
  local secret_kind=$2
  if [[ ! -f "$output_file" ]]; then
    umask 077
    python3 - "$output_file" "$secret_kind" <<'PY'
from __future__ import annotations

import base64
import secrets
import sys
from pathlib import Path

path = Path(sys.argv[1])
kind = sys.argv[2]
value = (
    base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    if kind == "base64-32"
    else secrets.token_urlsafe(48)
)
path.write_text(f"{value}\n", encoding="utf-8")
path.chmod(0o600)
PY
  fi
}

ensure_local_secret "$local_secret_dir/session-signing" token
ensure_local_secret "$local_secret_dir/credential-encryption" base64-32
ensure_local_secret "$local_secret_dir/freqtrade-password" token

# Local console startup is read-only regardless of values in a shared secret file.
export TRADING_DATABASE_URL="${TRADING_LOCAL_DATABASE_URL:-postgresql+psycopg://trading:local-trading-only@127.0.0.1:5434/trading_local}"
export TRADING_SESSION_SIGNING_SECRET="${TRADING_SESSION_SIGNING_SECRET:-$(<"$local_secret_dir/session-signing")}"
export TRADING_CREDENTIAL_ENCRYPTION_KEY="${TRADING_CREDENTIAL_ENCRYPTION_KEY:-$(<"$local_secret_dir/credential-encryption")}"
export TRADING_BINANCE_LIVE_ORDER_SEND_ENABLED=false
export TRADING_BINANCE_TESTNET_ORDER_SEND_ENABLED=false
export TRADING_HYPERLIQUID_LIVE_ORDER_SEND_ENABLED=false
export TRADING_HYPERLIQUID_TESTNET_ORDER_SEND_ENABLED=false
export TRADING_FREQTRADE_LIVE_ORDER_SEND_ENABLED=false
export TRADING_BINANCE_CAPITAL_WITHDRAW_ENABLED=false
export TRADING_ALLOW_MOCK_IDENTITY=false
export TRADING_EXECUTION_BACKEND=FREQTRADE
export TRADING_API_PORT="${TRADING_API_PORT:-8014}"
export TRADING_PUBLIC_BASE_URL="${TRADING_PUBLIC_BASE_URL:-http://127.0.0.1:${TRADING_API_PORT}}"
export TRADING_FREQTRADE_WORKERS_ENABLED="${TRADING_LOCAL_FREQTRADE_WORKERS_ENABLED:-false}"
export TRADING_FREQTRADE_API_USERNAME="${TRADING_FREQTRADE_API_USERNAME:-trading-control}"
export TRADING_FREQTRADE_API_PASSWORD="${TRADING_FREQTRADE_API_PASSWORD:-$(<"$local_secret_dir/freqtrade-password")}"

local_admin_password="${TRADING_LOCAL_ADMIN_PASSWORD:-}"
if [[ -z "$local_admin_password" ]]; then
  local_password_file="${TRADING_LOCAL_ADMIN_PASSWORD_FILE:-$PWD/.local/passwords/kelly_oooo}"
  if [[ ! -f "$local_password_file" ]]; then
    umask 077
    mkdir -p "$(dirname "$local_password_file")"
    python3 -c 'import secrets; print(secrets.token_urlsafe(24))' >"$local_password_file"
  fi
  local_admin_password="$(<"$local_password_file")"
fi
export TRADING_LOCAL_ADMIN_PASSWORD="$local_admin_password"

if [[ "$TRADING_FREQTRADE_WORKERS_ENABLED" == true ]]; then
  docker compose --profile execution-workers up -d \
    postgres \
    freqtrade-binance \
    freqtrade-hyperliquid
else
  docker compose up -d postgres
  docker compose stop freqtrade-binance freqtrade-hyperliquid >/dev/null
fi

for _ in {1..30}; do
  if docker compose exec -T postgres pg_isready -U trading -d trading_local >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

docker compose exec -T postgres pg_isready -U trading -d trading_local >/dev/null

if [[ "$TRADING_FREQTRADE_WORKERS_ENABLED" == true ]]; then
  for trading_worker_port in 8081 8082; do
    trading_worker_ready=false
    for _ in {1..60}; do
      if curl --silent --fail --max-time 2 \
        "http://127.0.0.1:${trading_worker_port}/api/v1/ping" >/dev/null; then
        trading_worker_ready=true
        break
      fi
      sleep 1
    done
    if [[ "$trading_worker_ready" != true ]]; then
      echo "Freqtrade dry-run worker on port ${trading_worker_port} did not become ready." >&2
      exit 1
    fi
  done
fi

uv run python scripts/setup_local.py
unset local_admin_password
unset TRADING_LOCAL_ADMIN_PASSWORD
trading_runtime_pid=""
if uv run python -c "from trading_control_plane.config import get_settings; raise SystemExit(0 if get_settings().runtime_sync_enabled else 1)"; then
  uv run trading-sync-worker &
  trading_runtime_pid="$!"
fi

trading_stop_runtime() {
  if [[ -n "$trading_runtime_pid" ]]; then
    kill "$trading_runtime_pid" >/dev/null 2>&1 || true
    wait "$trading_runtime_pid" >/dev/null 2>&1 || true
  fi
}
trap trading_stop_runtime EXIT INT TERM
uv run trading-api

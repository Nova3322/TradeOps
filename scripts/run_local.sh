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

# Local console startup is read-only regardless of values in a shared secret file.
export TRADING_BINANCE_LIVE_ORDER_SEND_ENABLED=false
export TRADING_BINANCE_TESTNET_ORDER_SEND_ENABLED=false
export TRADING_HYPERLIQUID_LIVE_ORDER_SEND_ENABLED=false
export TRADING_HYPERLIQUID_TESTNET_ORDER_SEND_ENABLED=false
export TRADING_FREQTRADE_LIVE_ORDER_SEND_ENABLED=false
export TRADING_EXECUTION_BACKEND=FREQTRADE

docker compose up -d postgres

for _ in {1..30}; do
  if docker compose exec -T postgres pg_isready -U trading -d trading_local >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

docker compose exec -T postgres pg_isready -U trading -d trading_local >/dev/null
uv run python scripts/setup_local.py
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

#!/usr/bin/env bash
set -euo pipefail

profiles=(--profile console)
notification_delivery=false
runtime_sync=false
for option in "$@"; do
  case "$option" in
    --runtime)
      if [[ $runtime_sync == true ]]; then
        echo "duplicate option: --runtime" >&2
        exit 2
      fi
      profiles+=(--profile runtime)
      runtime_sync=true
      ;;
    --notifications)
      if [[ $notification_delivery == true ]]; then
        echo "duplicate option: --notifications" >&2
        exit 2
      fi
      profiles+=(--profile notifications)
      notification_delivery=true
      ;;
    *)
      echo "usage: $0 [--runtime] [--notifications]" >&2
      exit 2
      ;;
  esac
done

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

runtime_dir="$project_root/.local/compose"
env_file="$runtime_dir/runtime.env"
local_admin_username="${TRADING_LOCAL_ADMIN_USERNAME:-trading-admin}"
if [[ ! "$local_admin_username" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$ ]]; then
  echo "TRADING_LOCAL_ADMIN_USERNAME must be a safe 1-120 character local identifier" >&2
  exit 2
fi
password_file="${TRADING_LOCAL_ADMIN_PASSWORD_FILE:-$project_root/.local/passwords/$local_admin_username}"
mkdir -p "$runtime_dir" "$(dirname "$password_file")"
chmod 700 "$project_root/.local" "$runtime_dir" "$(dirname "$password_file")"

if [[ ! -f "$env_file" ]]; then
  umask 077
  python3 - "$env_file" "$password_file" <<'PY'
from __future__ import annotations

import base64
import os
import secrets
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
password_path = Path(sys.argv[2])
admin_password = secrets.token_urlsafe(24)
credential_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
values = {
    "TRADING_DATABASE_URL": (
        "postgresql+psycopg://trading:local-trading-only@postgres:5432/trading_local"
    ),
    "TRADING_SESSION_SIGNING_SECRET": secrets.token_urlsafe(48),
    "TRADING_CREDENTIAL_ENCRYPTION_KEY": credential_key,
    "TRADING_FACT_ADAPTER_BEARER_TOKEN": secrets.token_urlsafe(48),
    "TRADING_LOCAL_ADMIN_PASSWORD": admin_password,
    "TRADING_LOCAL_ADMIN_USERNAME": os.environ.get(
        "TRADING_LOCAL_ADMIN_USERNAME", "trading-admin"
    ),
}
env_path.write_text(
    "".join(f"{key}={value}\n" for key, value in values.items()),
    encoding="utf-8",
)
password_path.write_text(f"{admin_password}\n", encoding="utf-8")
env_path.chmod(0o600)
password_path.chmod(0o600)
PY
fi

if ! grep -q '^TRADING_FACT_ADAPTER_BEARER_TOKEN=.' "$env_file"; then
  umask 077
  printf 'TRADING_FACT_ADAPTER_BEARER_TOKEN=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" >>"$env_file"
  chmod 600 "$env_file"
fi

required_variables=(
  TRADING_DATABASE_URL
  TRADING_SESSION_SIGNING_SECRET
  TRADING_CREDENTIAL_ENCRYPTION_KEY
  TRADING_FACT_ADAPTER_BEARER_TOKEN
  TRADING_LOCAL_ADMIN_PASSWORD
  TRADING_LOCAL_ADMIN_USERNAME
)
for variable in "${required_variables[@]}"; do
  if ! grep -q "^${variable}=." "$env_file"; then
    echo "compose runtime file is incomplete: ${variable}" >&2
    exit 2
  fi
done

echo "Local administrator password: $password_file"
echo "TradingOPS URL: http://127.0.0.1:${TRADING_PUBLIC_PORT:-8000}"
if [[ $notification_delivery == true ]]; then
  echo "Notification delivery: enabled by explicit --notifications profile"
else
  echo "Notification delivery: disabled (add --notifications to enable the worker)"
fi
if [[ $runtime_sync == true ]]; then
  echo "Read-only runtime synchronization: enabled by explicit --runtime profile"
else
  echo "Read-only runtime synchronization: disabled (add --runtime to enable the worker)"
fi
exec docker compose --env-file "$env_file" "${profiles[@]}" up --build

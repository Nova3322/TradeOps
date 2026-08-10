#!/usr/bin/env bash
set -euo pipefail

profiles=(--profile console)
notification_delivery=false
if [[ $# -gt 1 ]]; then
  echo "usage: $0 [--notifications]" >&2
  exit 2
fi
if [[ $# -eq 1 ]]; then
  if [[ $1 != "--notifications" ]]; then
    echo "usage: $0 [--notifications]" >&2
    exit 2
  fi
  profiles+=(--profile notifications)
  notification_delivery=true
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

runtime_dir="$project_root/.local/compose"
env_file="$runtime_dir/runtime.env"
password_file="$project_root/.local/passwords/kelly_oooo"
mkdir -p "$runtime_dir" "$(dirname "$password_file")"
chmod 700 "$project_root/.local" "$runtime_dir" "$(dirname "$password_file")"

if [[ ! -f "$env_file" ]]; then
  umask 077
  python3 - "$env_file" "$password_file" <<'PY'
from __future__ import annotations

import base64
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
    "TRADING_LOCAL_ADMIN_PASSWORD": admin_password,
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

required_variables=(
  TRADING_DATABASE_URL
  TRADING_SESSION_SIGNING_SECRET
  TRADING_CREDENTIAL_ENCRYPTION_KEY
  TRADING_LOCAL_ADMIN_PASSWORD
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
exec docker compose --env-file "$env_file" "${profiles[@]}" up --build

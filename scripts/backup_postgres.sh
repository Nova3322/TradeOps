#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: TRADING_DATABASE_URL=... $0 /absolute/path/trading.dump" >&2
  exit 2
fi
: "${TRADING_DATABASE_URL:?TRADING_DATABASE_URL is required}"

output=$1
if [[ "$output" != /* || "$output" != *.dump ]]; then
  echo "backup path must be an absolute .dump file" >&2
  exit 2
fi

mkdir -p "$(dirname "$output")"
umask 077
postgres_url=${TRADING_DATABASE_URL/postgresql+psycopg:/postgresql:}
if command -v pg_dump >/dev/null && command -v pg_restore >/dev/null; then
  pg_dump --format=custom --no-owner --no-acl --file="$output" "$postgres_url"
  pg_restore --list "$output" >/dev/null
elif [[ -n "${TRADING_PG_CONTAINER:-}" ]]; then
  database_name=$(uv run python -c 'import os; from sqlalchemy.engine import make_url; print(make_url(os.environ["TRADING_DATABASE_URL"]).database or "")')
  database_user=$(uv run python -c 'import os; from sqlalchemy.engine import make_url; print(make_url(os.environ["TRADING_DATABASE_URL"]).username or "")')
  docker exec "$TRADING_PG_CONTAINER" pg_dump -U "$database_user" --format=custom --no-owner --no-acl "$database_name" >"$output"
  docker exec -i "$TRADING_PG_CONTAINER" pg_restore --list <"$output" >/dev/null
else
  echo "pg_dump/pg_restore are required; local containers may set TRADING_PG_CONTAINER" >&2
  exit 4
fi
echo "verified PostgreSQL custom-format backup: $output"

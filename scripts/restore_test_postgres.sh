#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: TRADING_DATABASE_URL=... $0 /absolute/path/trading.dump" >&2
  exit 2
fi
: "${TRADING_DATABASE_URL:?TRADING_DATABASE_URL is required}"

archive=$1
if [[ ! -f "$archive" ]]; then
  echo "backup archive does not exist" >&2
  exit 2
fi

database_name=$(uv run python -c 'import os; from sqlalchemy.engine import make_url; print(make_url(os.environ["TRADING_DATABASE_URL"]).database or "")')
if [[ "$database_name" != *_test ]]; then
  echo "restore is restricted to an explicitly disposable *_test database" >&2
  exit 3
fi

postgres_url=${TRADING_DATABASE_URL/postgresql+psycopg:/postgresql:}
if command -v pg_restore >/dev/null; then
  pg_restore --clean --if-exists --no-owner --no-acl --dbname="$postgres_url" "$archive"
elif [[ -n "${TRADING_PG_CONTAINER:-}" ]]; then
  database_user=$(uv run python -c 'import os; from sqlalchemy.engine import make_url; print(make_url(os.environ["TRADING_DATABASE_URL"]).username or "")')
  docker exec -i "$TRADING_PG_CONTAINER" pg_restore -U "$database_user" --clean --if-exists --no-owner --no-acl --dbname="$database_name" <"$archive"
else
  echo "pg_restore is required; local containers may set TRADING_PG_CONTAINER" >&2
  exit 4
fi
uv run alembic check >/dev/null
uv run python -c 'import os; from trading_control_plane.database import Database; database=Database(os.environ["TRADING_DATABASE_URL"]); ready=database.is_ready(); database.dispose(); assert ready == (True, None), ready'
echo "restored and verified disposable database: $database_name"

# Contributing to TradingOPS

Thank you for improving a fail-closed trading governance system. Contributions
must preserve the operational and licensing boundaries below.

## Development setup

```bash
cp .env.example .env.local
uv sync --frozen
./scripts/run_local.sh
```

The local launcher generates private runtime secrets under `.local/`, creates a
local administrator named by `TRADING_LOCAL_ADMIN_USERNAME`, forces every
external side-effect switch off, and uses a PostgreSQL database isolated from
production.

## Change rules

- Reuse the existing User, Workspace, Team, Account, Proposal, Approval, Risk,
  execution, receipt, and audit sources before adding another entity or store.
- Server-side authorization, independent review, scope, risk, idempotency, and
  audit checks are authoritative. UI visibility is not authorization.
- Keep TESTNET and LIVE explicit and isolated across accounts, credentials,
  proposals, authorizations, orders, positions, capital facts, and analytics.
  `SETUP` is not an execution environment, and removed simulation paths must not
  return as compatibility fallbacks. Missing, stale, or rate-limited data is
  neither real-time data nor zero.
- New external side effects require durable idempotency, query-before-retry,
  unknown-outcome handling, reconciliation, audit, and disabled-by-default
  process and database gates.
- Never commit secrets, `.env.local`, `.local/`, database dumps, private
  strategies, account identifiers, balances, unredacted logs, or raw screenshots.
- Use synthetic fixture names and values in code, tests, docs, and screenshots.

## Contributor License Agreement

Every contribution requires acceptance of [`CLA.md`](CLA.md). The CLA does not
transfer copyright; it gives the project the rights required for the
`GPL-3.0-only OR LicenseRef-TradingOPS-Commercial-1.0` dual-license model.

Do not submit employer-owned, customer-owned, third-party, or encumbered
material without written authority. Identify third-party code and licenses in
the pull request.

## Verification

```bash
UV_CACHE_DIR=/tmp/tradingops-uv uv sync --frozen
uv run ruff check .
uv run mypy src
TEST_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST:PORT/trading_test' uv run pytest
python scripts/generate_publication_metadata.py --check
python scripts/verify_public_release.py
```

Use a disposable PostgreSQL database whose name ends in `_test`. Never point
migrations, restores, tests, screenshots, or fixtures at a shared or production
database.

## Pull requests

Keep changes small, document code/API/page/runtime/test evidence, and state any
migration, security, license, privacy, or compatibility impact. Maintainers may
request a threat model for changes touching identity, credentials, risk,
execution, capital, signing, or external side effects.

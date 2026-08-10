# Contributing to TradingOPS

## Development setup

```bash
uv sync --frozen
./scripts/run_compose.sh
```

Use a disposable PostgreSQL database whose name ends in `_test` for integration tests. Do not point
tests, restore scripts, migrations, screenshots, or fixtures at a production or shared trading
database.

## Change rules

- Reuse the existing User, Team, Account, Proposal, Approval, Risk, execution, receipt, and audit
  sources before adding an entity or state store.
- Server-side authorization, risk, idempotency, account scope, and audit checks are mandatory; UI
  hiding is not enforcement.
- Keep SHADOW, TESTNET, and LIVE facts explicit and isolated. Missing or stale data is not zero or
  live data.
- New external side effects require stable idempotency, query-before-retry, unknown-outcome handling,
  versioned evidence, and disabled-by-default process and database gates.
- Never commit secrets, dumps, `.env.local`, `.local/`, private screenshots, account identifiers, or
  unredacted logs.

## Contributor license grant

By intentionally submitting a contribution to TradingOPS, you represent that you have the right to
submit it and grant the TradingOPS project maintainers, acting as licensors for the project, a
perpetual, worldwide, royalty-free, irrevocable, non-exclusive copyright license to reproduce,
modify, prepare derivative works of, publicly display, publicly perform, distribute, sublicense, and
relicense the contribution as part of TradingOPS under both:

1. the Combined Community License identified in `LICENSE`; and
2. separately executed TradingOPS Commercial Licenses.

You also grant a corresponding patent license for patent claims you can license that are necessarily
infringed by the contribution alone or by its combination with TradingOPS. This grant does not
transfer your copyright ownership. It does allow the project to maintain the dual-license model,
including paid organizational, SaaS, hosted, white-label, and resale scopes.

Submission through a pull request or another intentional contribution channel constitutes acceptance
of this grant. Do not submit employer-owned, client-owned, third-party, or encumbered material unless
you have written authority to make this grant. The pull request template records the contributor's
confirmation.

## Verification

```bash
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src
TEST_DATABASE_URL='postgresql+psycopg://.../trading_test' uv run pytest
uv run trading-doctor --skip-database
docker compose --env-file .local/compose/runtime.env --profile console config --quiet
```

Changes to migrations, APIs, pages, or operational behavior must include the corresponding database,
backend, actual-page/runtime, and automated-test evidence. Keep unrelated untracked artifacts out of
the commit.

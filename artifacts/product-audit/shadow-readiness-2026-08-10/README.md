# Shadow readiness guidance acceptance

## Scope

- Route: `/shadow` on the current local Compose runtime at `http://127.0.0.1:8022`.
- Browser viewport: 1422 x 800 CSS pixels.
- Identity and scope: current authenticated system administrator in `Default Workspace / Default Team`.
- No proposal, review, order, funding, signing, broadcast, or risk-policy mutation was submitted.

## Runtime facts

- The page renders four grouped readiness steps from the blocker codes returned by `/api/shadow`.
- Members/duties, signal source, and exchange-account scope are satisfied.
- Versioned risk is blocked by `RISK_LIMITS_REQUIRED`; the single primary action opens `/risk`.
- An unknown future blocker is retained as an explicit fail-closed row instead of being discarded.
- Document-level horizontal overflow is false; browser console log count is zero.
- Live order sending, funding, signing, and broadcast remain closed.

## Evidence

- `01-shadow-readiness-dark.jpg`: dark-theme current state.
- `02-shadow-readiness-light.jpg`: light-theme parity for the same facts and action.

## Automated checks

- JavaScript and service-worker syntax passed.
- Web design-token and API shell tests: 23 passed.
- Shadow HTTP API, activation scope, and served-page integration test: 1 passed on the disposable `trading_test` database.
- Docker Compose rebuilt the current image; `/health/ready` returned PostgreSQL ready.

## Remaining acceptance

- Exact 1024, 430, and 390 CSS-pixel browser captures are part of stage 9.2.
- Keyboard-only traversal and screen-reader announcements across the full four-flow matrix remain pending.

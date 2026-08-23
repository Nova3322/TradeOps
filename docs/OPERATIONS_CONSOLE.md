# Operations console behavior

This document records the current page ownership and safety behavior implemented
by the local TradeOps console. The running `/openapi.json` remains the complete
API contract; this guide describes how the product exposes that contract.

## Execution mode

The user-visible modes are:

- **TESTNET / Test mode** — eligible orders are sent only to the configured
  exchange test endpoint.
- **LIVE / Production mode** — eligible orders use production adapters and may
  affect real funds, but only after every independent capability gate passes.

`SETUP` is an internal state for a Team that has not selected an execution mode.
It is not an account, proposal, authorization, order, position, or capital-fact
environment. The retired SHADOW simulator is not available as a mode, hidden
route, API fallback, or virtual-balance source.

The header **Current mode** control is the only control that changes the Team
mode. Administrators with `team.manage` receive target readiness,
execution-readiness advisories, and the final confirmation control. Production
confirmation uses the exact phrase shown by the UI. The request also carries
`expected_version` and an idempotency key, and the result is audited.

A successful switch does not enable live order send, automatic position adds,
capital transfer, signing, or broadcast. Unexecuted authorizations and order
intents from the source environment are invalidated; historical proposals retain
their original environment.

## Account Management

Account Management can show and configure TESTNET and LIVE accounts before a
mode switch. Its environment control changes only the configuration view; it
does not change the Team mode.

Each account is isolated by Team, environment, Venue, and account ID. The page
supports:

- create and rename;
- credential rotation without secret echo;
- connection verification;
- enable and disable;
- deletion with explicit confirmation and reference checks; and
- runtime and trading-readiness status.

Deletion fails closed while a proposal, authorization, order intent, venue
order, open or unknown position, capital task, runtime binding, or other business
reference still depends on the account. Execution services derive the active
environment from Team mode rather than trusting a client-supplied environment.

## Signals

Perptape and Webhook are separate signal products:

- **Perptape** presents one opportunity card per symbol and direction, aggregates
  breakout periods from the current snapshot, and keeps filters in a compact
  drawer. Long symbols use the restrained positive color and short symbols the
  restrained negative color. **Breakout details** opens the Perptape market
  scanner for the exact exchange and raw venue symbol (for example,
  `PROVEUSDT`); the query does not prepend exchange or canonical-symbol
  identifiers. Exchange charts remain a separate link.
- **Webhook Signals** lists only events that passed the configured source's
  signature, request-time, nonce/replay, idempotency, size, version, and format
  checks. Stale records remain visible as facts but cannot silently become
  review or execution authorization.
- **Signal Sources** keeps the server-source summary and each source card
  collapsed by default. Expanding a card reveals redacted health, connection,
  edit, credential-rotation, test, disable, and delete controls allowed by RBAC.

Signal freshness or market-data readiness is not proposal eligibility. Proposal
creation revalidates exact instrument, Team mode, account scope, policy, and
current facts on the server.

## Review Queue and Trade History

- **Review Queue** is sorted by proposal creation time descending so the newest
  record appears first. It filters by environment, instrument, direction, risk,
  and source or status.
- **Trade History** is the user-facing name of the existing `/campaigns`
  destination. It retains authorization, risk reservation, orders, fills,
  protection, reconciliation, and final outcomes for active and closed trades;
  filters cover instrument/account, direction, Venue, and status.
- Both lists default to 50 records per page and allow 100. No page-size option
  above 100 is exposed.
- Once the independent-review threshold is met, the system automatically runs
  current-fact risk checks, issues short-lived authorization, reserves risk, and
  calls the exact-account Freqtrade worker within lease, fencing, and idempotency
  boundaries. Timeout or ambiguity is query-only and never resubmits the order.

## Capital Center and Performance Reports

The Capital Center owns production treasury configuration and direct-capital
workflow status. NoTilt Vault and Safe Spending Limits can be configured at the
same time. One provider is selected as the current provider for newly created
operations; switching the selection preserves the other provider's configuration.
Each operation freezes its selected provider and rechecks address, network,
asset, amount, fee, allowance, receipt, and capability gates.

The browser form accepts public addresses and account scope only. It does not
request wallet private keys, seed phrases, wallet passwords, or signing tokens.
Human wallets or valid multisig policy remain the signing and broadcast boundary.
TESTNET does not expose production Vault, Safe, withdrawal, or capital-transfer
configuration.

Performance Reports owns account-equity history and charts:

- only accounts from the current Team mode are selectable;
- account selection changes report display only;
- independent series remain visible when aggregation is not trustworthy;
- missing, stale, misaligned, or disconnected data is not filled with zero;
- aggregate lines are rendered only when the underlying facts are compatible;
- gaps remain gaps rather than forced continuity; and
- the chart supports range selection and a fullscreen view that uses the
  available viewport for the plot instead of enlarging surrounding chrome.

## Risk, notifications, and shell

- **Risk Center** separates routine policy editing from live safety blockers.
  Routine controls are collapsed until requested. Direct administrator changes
  and proposal/review changes continue to follow their distinct permission and
  audit rules.
- **Notification Center** owns Telegram, Slack, Lark, and email routes. Route
  credentials are encrypted and redacted; source/account pages do not duplicate
  notification-account configuration. Recent deliveries filter by event/scope,
  channel, status, and environment and paginate at 50 records by default or 100
  maximum. Notification delivery has no trading, capital, signing, or broadcast
  interface.
- The desktop navigation can collapse to the left and mobile navigation uses a
  modal drawer. The header **Current mode** control is visually joined to the
  theme control; server permission, readiness, version, idempotency, and audit
  checks remain authoritative.

The console supports Chinese and English, light and dark themes, keyboard focus,
responsive layouts, and reduced-motion behavior. UI visibility never replaces
server authorization.

## Verification boundary

For an isolated local console with read-only runtime synchronization:

```bash
TRADING_PUBLIC_PORT=8022 ./scripts/run_compose.sh --runtime
curl --fail http://127.0.0.1:8022/health/live
curl --fail http://127.0.0.1:8022/health/ready
```

Do not use a successful build, a visible button, or a healthy process as proof
that an exchange account, order-send path, or capital path is ready. Verify the
real API response and page state, and keep real LIVE order submission outside
automated acceptance.

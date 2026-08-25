# Unified production configuration

TradeOps keeps installation defaults fail-closed. A deployment may define one
reviewed, non-secret `production.yaml` and then reconcile it through the public
CLI:

```bash
tradeops-config validate production.yaml
tradeops-config apply production.yaml
tradeops-config status production.yaml
```

- `validate` checks the schema, exact team/workspace/account identities, LIVE
  Workers and facts, reconciliation, risk, Perptape, Telegram, Safe/capital
  runtime and Gate prerequisites. Mutable differences are reported as `DRIFT`;
  unsafe or missing prerequisites are `BLOCKED`.
- `apply` uses existing `TradingService` RBAC, audit, version and idempotency
  boundaries. It does not use direct SQL. Gates are written last, only after all
  other configured preconditions pass.
- `status` compares the reviewed intent with current database and runtime facts.
  It exits non-zero for either drift or a blocker.

## Required sections

The root schema version is `1`. The file contains:

- `operator_username`: an active HUMAN with `SYSTEM_ADMIN` in the exact team;
- `team`: exact `team_id`, `workspace_id`, `trading_enabled` and `LIVE` or
  `TESTNET` mode;
- `gates`: explicit states and audited reasons for `LIVE_ORDER_SEND`,
  `CAPITAL_TRANSFER`, `AUTO_ADD`, `AUTO_OPERATING_REFILL` and
  `AUTO_PROFIT_SWEEP`;
- `accounts`: exact Binance/Hyperliquid account IDs, environment, eligibility,
  fact/reconciliation freshness and required Freqtrade mode/fingerprint; the
  Hyperliquid entry also fixes the account HIP-3 DEX scope;
- `risk`: the versioned risk-policy identity, single/account/portfolio loss
  limits, consecutive-loss cooldown, maximum position notional, profitable-add
  spacing, Bollinger midline reference periods, per-tier add counts, and
  per-tier total campaign-loss limits;
- `proposals`: exact default account ID, notional, maximum risk, risk tier,
  invalidation, expiry and automatic-proposal settings;
- `perptape`: exact signal-source ID, enabled state and feed freshness;
- `telegram_routes`: exact route IDs, subscriptions and, for proposal-review
  routes, both the internal reviewer username and exact private Telegram
  username;
- `capital_runtime`: non-secret Safe/Binance continuation switches and RPC
  endpoints;
- `capital`: exact provider, accounts, addresses, four permitted paths and
  amount/fee limits;
- `capital_automation_policies`: explicit policy list.

The current service rejects LIVE capital automation policies. Use an explicit
empty list and keep `AUTO_OPERATING_REFILL` and `AUTO_PROFIT_SWEEP` disabled.
This preserves the existing fail-closed semantics instead of claiming that an
unsupported automatic transfer policy is ready.

The position/add fields in `risk` are optional as a group for an existing
deployment whose `AUTO_ADD` Gate remains disabled. If any one is supplied, the
complete group is required. The service accepts only limits at or below the
supported LOW/MEDIUM/HIGH safety ceilings (1/2/3 adds and 0.5%/1.0%/1.5% total
campaign loss). Enabling `AUTO_ADD` remains a separate reviewed action and is
blocked until this versioned position policy is complete.

## Secret boundary

Do not place API keys, API secrets, private keys, passwords, bot tokens,
database URLs, signing secrets or credential ciphertext in this file. The
loader rejects secret-like field names. Exchange, Perptape and Telegram
credentials remain encrypted in PostgreSQL; production runtime secrets remain
in the server secret file.

Telegram reviewer binding is not inferred from a display name. During `apply`,
the existing encrypted route is queried with Telegram `getChat`; the route must
be a private chat whose username exactly matches the configured reviewer. The
numeric chat is then bound through the existing audited service method. Group
chats and ambiguous identities remain blocked.

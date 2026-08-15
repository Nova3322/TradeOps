# Architecture and safety boundary

TradingOPS sits between strategy engines and real execution. Strategies produce
candidate intent; TradingOPS turns permitted intent into frozen, reviewable,
auditable operations; execution adapters perform only explicitly authorized
side effects.

```text
strategy / signal engines
          |
          v
source + freshness validation
          |
          v
frozen proposal -> independent review -> risk and scope gates
          |                                  |
          | rejected / expired               | authorized
          v                                  v
audit trail                         idempotent execution adapter
                                               |
                                               v
                                  venue / treasury provider
                                               |
                                               v
                                  reconciliation + evidence
```

## Authoritative boundaries

- PostgreSQL is the durable source for identity, Workspace/Team membership,
  roles, accounts, proposals, reviews, gates, receipts, and audit events.
- Workspace and Team membership is checked server-side. Account and Venue scope
  cannot be widened by client parameters.
- The proposer and reviewer are independent subjects; self-review is rejected.
- Requests that may create external effects require durable idempotency and
  explicit handling of unknown outcomes.
- Runtime execution facts and side effects are isolated between TESTNET and
  LIVE. `SETUP` is a Team configuration state only; it is not an execution
  environment. The retired SHADOW ledger is absent from current domain enums,
  services, routes, and database head.
- Missing, stale, lost, or rate-limited data blocks unsafe claims and actions.
- Client UI, API prompts, and Agent role names never override server policy.

## Mode and account boundary

- **Mode Settings** is the only console page that changes the Team execution
  mode. Switching requires `team.manage`, an interactive session, confirmation,
  `expected_version`, idempotency, readiness checks, and audit evidence.
- **Account Management** can configure TESTNET and LIVE accounts ahead of time,
  but its environment selector is only a configuration filter. Execution always
  derives the environment from the Team's persisted current mode.
- Account and credential identity is scoped by Team, environment, Venue, and
  account ID. TESTNET credentials never load into LIVE adapters and LIVE
  credentials never load into TESTNET adapters.
- A mode switch invalidates unexecuted authorization and intent from the source
  environment without rewriting historical proposal environments. It does not
  enable `LIVE_ORDER_SEND`, automation, or capital movement.

## Console data ownership

- Perptape opportunities and signed Webhook signals remain separate feeds.
  Perptape detail links open the upstream market scanner; Webhook freshness and
  proposal eligibility remain server facts.
- The Capital Center owns production NoTilt Vault and Safe Spending Limits
  configuration. Both providers may remain configured while one is selected for
  each newly frozen direct-capital operation.
- Performance Reports owns account-equity history, trusted aggregation, gap
  rendering, range selection, and fullscreen chart presentation. Selecting
  accounts changes only the report display.

See [Operations console behavior](OPERATIONS_CONSOLE.md) for the page-level
contract.

## Default side-effect state

`AUTO_ADD`, `AUTO_OPERATING_REFILL`, `AUTO_PROFIT_SWEEP`, `CAPITAL_TRANSFER`,
and `LIVE_ORDER_SEND` are persistent gates and must remain `DISABLED` in a new
installation. Transport-specific switches are also disabled. Enabling one
layer never implies another layer is ready.

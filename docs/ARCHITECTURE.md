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
- SHADOW, TESTNET, and LIVE facts and side effects remain separate.
- Missing, stale, lost, or rate-limited data blocks unsafe claims and actions.
- Client UI, API prompts, and Agent role names never override server policy.

## Default side-effect state

`AUTO_ADD`, `AUTO_OPERATING_REFILL`, `AUTO_PROFIT_SWEEP`, `CAPITAL_TRANSFER`,
and `LIVE_ORDER_SEND` are persistent gates and must remain `DISABLED` in a new
installation. Transport-specific switches are also disabled. Enabling one
layer never implies another layer is ready.

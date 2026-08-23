# Architecture and safety boundary

TradeOps is the control layer between traders, trading bots, strategy programs,
AI agents, and exchange accounts. Those actors submit candidate intent; TradeOps
turns permitted intent into frozen, reviewable, auditable operations; execution
adapters perform only explicitly authorized side effects.

```text
trader / trading bot / strategy program / AI agent
          |
          v
source + freshness validation
          |
          v
frozen proposal -> deterministic policy -> approve / review / reject
          |                                  |
          | rejected / expired               | authorized
          v                                  v
  audit trail                  lease/fencing + Freqtrade worker
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
- Trader, Reviewer, Risk Manager, and Administrator are distinct human
  responsibilities. Administrative scope does not create an implicit risk,
  review, or execution bypass.
- Requests that may create external effects require durable idempotency and
  explicit handling of unknown outcomes.
- Runtime execution facts and side effects are isolated between TESTNET and
  LIVE. `SETUP` is a Team configuration state only; it is not an execution
  environment. The retired SHADOW ledger is absent from current domain enums,
  services, routes, and database head.
- Missing, stale, lost, or rate-limited data blocks unsafe claims and actions.
- Client UI, API prompts, and Agent role names never override server policy.
- Deterministic services make policy decisions. Humans, bots, strategy programs,
  and AI agents submit intent, but none decides whether server policy or required
  approval can be skipped.
- TradeOps owns proposals, review, RBAC, risk, authorization, execution intent,
  reconciliation, and audit. Freqtrade is the sole strategy/bot lifecycle engine;
  `ControlPlaneOnlyStrategy` does not originate orders outside authorized TradeOps
  intent. The Facts Adapter remains authoritative for observed exchange state.

## Deployment boundary

- The application is self-hostable. Exchange credentials are encrypted at rest
  and are not returned after submission; trading adapters do not need withdrawal
  permission.
- The public repository is the source and build boundary. After public `main`
  CI succeeds, its release workflow builds a single GHCR OCI image index for
  `linux/amd64` and `linux/arm64`, labels the images with the tested full source
  SHA, version, and Schema Revision, and publishes SBOM and provenance
  attestations. It contains no production host or deployment configuration.
- Production deployment is a separate private-operations boundary. The private
  Ops repository pins the public source SHA and exact multi-platform manifest
  digest in `release.yaml`; mutable tags and short SHAs are rejected. A
  `pending` contract remains non-deployable, and an `active` promotion requires
  private review plus the GitHub `production` Environment approval.
- The production host pulls by manifest digest and verifies the selected image
  architecture and release labels before migration. GitHub Actions and systemd
  use the same successfully staged immutable Ops revision, so a host restart
  cannot silently return to a stale public checkout.
- This repository does not currently package a separate Local Execution Agent
  with hard limits that a remote control plane is technically unable to bypass.
  That architecture must not be presented as an available guarantee.

## Mode and account boundary

- The header **Current mode** control is the only console control that changes
  the Team execution mode. Switching requires `team.manage`, an interactive
  session, confirmation, `expected_version`, idempotency, readiness checks, and
  audit evidence.
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

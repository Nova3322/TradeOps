# TradeOps documentation

TradeOps is the open-source trading control layer between traders, trading bots,
AI agents, and exchange accounts. The same workflow applies to manual and
automated trades: request, frozen proposal, deterministic policy check, decision,
controlled execution, exchange reconciliation, and audit.

It is for professional traders, developers, and small crypto trading teams that
need to limit what automated systems and team members may do with real accounts.
It is not a market-data terminal, strategy backtester, copy-trading service,
custodian, fund PMS, or investment adviser.

## Start here

- [Run the project locally](../README.md#run-locally)
- [Operations console](OPERATIONS_CONSOLE.md)
- [API and bot quickstart](API_QUICKSTART.md)
- [AI/API usage](AI_API_QUICKSTART.md)
- [Architecture and trust boundaries](ARCHITECTURE.md)
- [Security policy](../SECURITY.md)

## How the current release works

1. A human, trading bot, or AI agent submits a proposal.
2. Server-side policy checks the team, account, environment, data freshness,
   and configured risk limits.
3. Deterministic policy chooses automatic approval, independent review, or
   rejection. In the current Alpha, executable proposals still require manual
   independent review and self-review is blocked.
4. When the independent-review threshold is met, TradeOps automatically runs
   current-fact risk checks, issues short-lived authorization, and reserves risk.
5. The controlled executor uses leases, fencing, and a stable client order ID
   to call the exact-account Freqtrade worker in the proposal's fixed TESTNET or
   LIVE environment.
6. The Facts Adapter queries and reconciles the exchange outcome. Unknown or
   timed-out results remain query-only and are never blindly resubmitted.

Every human, trading bot, strategy program, and AI agent should have a separate
identity, permission set, risk allowance, and audit history. Trader, Reviewer,
Risk Manager, and Administrator responsibilities stay distinct. No actor,
including an administrator, gets an undocumented path around policy, capability
gates, account scope, or environment isolation.

## Capability status

| Area | Available now | Not implied |
| --- | --- | --- |
| Proposal and review | Versioned proposals, approve/reject, no self-review, expiry | Automatic approval is a workflow outcome but is not enabled in the current Alpha |
| Risk control | Team/account/single-trade limits, cooldown, no-pyramid, reduce-only, pause, kill switch | No promise that trading losses are prevented |
| Execution | Exact-account Binance and Hyperliquid Freqtrade workers, idempotency, fencing, cancel, query-only recovery, Facts sync, and reconciliation | LIVE send is not enabled by installation or process health |
| Signals | Perptape and signed Webhook ingestion with freshness and replay checks | Not a signal marketplace or trading recommendation |
| Notifications | Telegram team routes and optional channel adapters | Delivery configuration is deployment-specific |
| Bot and Agent API | User-owned API Keys, RBAC, workspace/team scope, proposal access | Agents cannot bypass human review or execute outside scope |
| Capital | Vault/Safe configuration and controlled capital-path gates | TradeOps is not a custodian; signing, broadcast, and transfer are separate capabilities |

Formal turnkey contracts for Hummingbot, NautilusTrader, and QuantConnect LEAN
are roadmap items. A separately packaged Local Execution Agent
with non-bypassable local hard limits is a design direction, not a current
product claim.

## Reference

- [Local configuration template](../.env.example)
- [Unified production configuration](PRODUCTION_CONFIGURATION.md)
- [Publication boundary](PUBLICATION_BOUNDARY.md)
- [Release process](RELEASING.md)
- [Competitive positioning](COMPETITIVE_POSITIONING.md)
- [Roadmap](../ROADMAP.md)

# TradeOps positioning and adjacent products

TradeOps is the open-source trading control layer between traders, trading bots,
AI agents, and exchange accounts. Its job is to decide whether a proposed
trade is allowed, determine who must approve it, execute only within fixed scope,
reconcile the venue result, and retain an audit trail.

That is a narrower problem than building a complete trading platform:

- A trading terminal answers **where a person trades**.
- A strategy engine or execution algorithm answers **what to trade** or **how
  to execute it**.
- A wallet permission system answers **who may move funds**.
- TradeOps answers **whether this trade may execute, who must approve it,
  which rule blocked it, and what actually happened at the exchange**.

## Integration ecosystem

| Project/platform | Primary focus | Relationship to TradeOps |
| --- | --- | --- |
| [Freqtrade](https://docs.freqtrade.io/en/latest/) | Open-source crypto bot, strategy, backtesting, optimization, and exchange connectivity | A possible strategy/execution source. A formal turnkey contract remains roadmap work. |
| [Hummingbot](https://hummingbot.org/docs/) | Automated market-making framework and exchange/blockchain connectors | A possible strategy and connectivity source. A formal turnkey contract remains roadmap work. |
| [NautilusTrader](https://nautilustrader.io/docs/latest/) | Event-driven research, simulation, portfolio/risk modeling, and live execution engine | A possible engine integration. TradeOps does not replace its research or execution model. |
| [QuantConnect LEAN](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/algorithm-engine) | Research, backtesting, portfolio/data management, brokerage integration, and live trading | A possible engine integration. TradeOps remains the proposal, approval, authorization, and audit boundary. |
| [Fireblocks](https://www.fireblocks.com/platforms/governance-and-policies) | Wallet/key infrastructure, transaction policy, approval, authorization, and audit | Adjacent in governance. TradeOps is not a custodian and does not replace wallet/key infrastructure. |
| [Talos](https://www.talos.com/) | Broad institutional connectivity, execution, portfolio, settlement, and post-trade platform | A substantially broader platform. TradeOps is narrower self-hosted control-layer software. |

TradeOps should not be described as a full replacement for Talos, Fireblocks,
Elwood, or a fund PMS. It also is not a market-data terminal, strategy
marketplace, copy-trading service, autonomous profit bot, custodian, or
investment adviser.

## Current differentiators

- Frozen, versioned proposals instead of mutable trade intent.
- Independent review with self-review prevention.
- Server-side workspace, team, environment, venue, and account enforcement.
- Deterministic risk checks and persistent capability gates.
- Idempotent commands, timeout/unknown-outcome recovery, and reconciliation.
- TESTNET/LIVE provenance and fail-closed data freshness semantics.
- Audit records connecting the actor and decision to exchange execution.
- Separate identity, permission, allowance, and audit history for each human,
  bot, strategy program, and AI agent.

This is an architectural comparison, not a claim of feature parity. Verify each
linked product's current documentation before procurement or integration.

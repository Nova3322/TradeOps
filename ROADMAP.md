# TradeOps Roadmap

Roadmap items describe intent, not commitments. Every milestone preserves
fail-closed defaults and server-side enforcement.

## Current product baseline

- TESTNET and LIVE proposal, independent review, deterministic risk,
  authorization, execution, reconciliation, and audit workflow
- Binance and Hyperliquid adapter paths with environment-scoped credentials
- Perptape and signed Webhook signal intake
- user-owned identities and API Keys for human, bot, strategy-program, and
  AI-agent proposal access
- Telegram notification routes and optional additional channel adapters
- self-hosted operations console with dangerous capabilities disabled by default

These items still require deployment-specific credentials, provider readiness,
and operator acceptance. A separately packaged Local Execution Agent with
non-bypassable local hard limits is not part of the current baseline.

## 0.1 — Public foundation

- GPL-3.0-only plus commercial dual licensing
- sanitized public repository boundary, SBOM, third-party notices
- five-minute local startup and AI/API quickstart
- CI, dependency updates, secret scanning, release checklist

## 0.2 — Integration contracts

- versioned adapters for strategy engines and execution workers
- contract tests for Freqtrade, Hummingbot, NautilusTrader, and LEAN-style inputs
- improved data-source freshness and provenance reporting

## 0.3 — Operational evidence

- exportable approval, risk, execution, and reconciliation evidence bundles
- recovery drills and unknown-outcome workflows
- accessibility and responsive regression automation

## Later candidates

- additional venue adapters behind explicit gates
- policy-as-code authoring with review and rollback
- enterprise identity and audit export integrations

Not planned: a strategy marketplace, custodial wallet, exchange, or guaranteed
profit system.

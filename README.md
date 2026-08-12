# TradingOPS

**Fail-closed trading governance and operations control plane**

[简体中文](README.zh-CN.md) · [API quickstart](docs/API_QUICKSTART.md) ·
[Architecture](docs/ARCHITECTURE.md) · [Security](SECURITY.md) ·
[License](#license)

TradingOPS sits between strategy engines and real execution. It turns candidate
intent into frozen proposals, independent review, limited authorization,
risk-gated execution, audit evidence, and capital reconciliation.

> It is not a strategy marketplace, custodial wallet, exchange, or automatic
> profit system.

![TradingOPS API access using synthetic fixtures](artifacts/public/api-access-1440.png)

## Why it exists

Trading engines are good at research, signals, portfolio logic, and venue
connectivity. Real operations also need separation of duties, immutable decision
inputs, exact account scope, server-side risk checks, replay-safe commands,
unknown-outcome recovery, and evidence that connects approval to execution.
TradingOPS owns that governance boundary without becoming a second strategy or
execution truth source.

## Core workflow

```text
strategy or signal
      -> source/freshness validation
      -> frozen proposal
      -> independent review
      -> scope + risk + capability gates
      -> idempotent execution adapter
      -> reconciliation and audit evidence
```

- **Workspace / Team isolation** — membership, roles, accounts, and records are
  checked server-side at every boundary.
- **Frozen proposals** — material terms are versioned before review.
- **Independent review** — a proposer cannot approve its own proposal.
- **Fail-closed risk** — missing, stale, lost, incomplete, or rate-limited data
  is neither real-time data nor zero.
- **Controlled execution** — external effects require explicit process and
  persistent database gates, idempotency, and reconciliation.
- **Human-owned API Clients** — Tokens inherit current roles dynamically and are
  fixed to one Workspace, Team, Account, and Venue.

## Five-minute safe start

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Docker, and Docker
Compose.

```bash
git clone https://github.com/nineheavens223-sys/TradeOps.git
cd TradeOps
cp .env.example .env.local
export TRADING_LOCAL_ADMIN_USERNAME=trading-admin
uv sync --frozen
./scripts/run_local.sh
```

Open <http://127.0.0.1:8014>. The generated local password is stored at:

```text
.local/passwords/trading-admin
```

The file is mode `0600`, `.local/` is ignored, external integrations are off,
and every dangerous capability gate remains disabled. Start with read-only data
and SHADOW workflows. Do not add exchange or wallet credentials until you have
reviewed [`SECURITY.md`](SECURITY.md) and the runtime boundary.

### Health and API

```bash
curl http://127.0.0.1:8014/health/live
curl http://127.0.0.1:8014/health/ready
open http://127.0.0.1:8014/openapi.json
```

API/AI onboarding is in [`docs/API_QUICKSTART.md`](docs/API_QUICKSTART.md) and
[`docs/AI_API_QUICKSTART.md`](docs/AI_API_QUICKSTART.md). The running
`/openapi.json` document is the only complete interface contract.

## Architecture

TradingOPS is a Python/FastAPI application with PostgreSQL durability, a
server-rendered JavaScript operations console, explicit venue/treasury adapters,
and separate optional workers. PostgreSQL remains authoritative for identity,
roles, scope, proposals, reviews, gates, receipts, and audit events.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the boundary diagram and
safety invariants.

## What TradingOPS is — and is not

- Freqtrade, Hummingbot, NautilusTrader, and QuantConnect LEAN primarily provide
  strategy, simulation, portfolio, connector, and/or execution-engine
  capabilities. TradingOPS treats engines as an integration ecosystem.
- Fireblocks overlaps in policy and approval but also provides wallet and key
  infrastructure; TradingOPS is not a custodian.
- Talos is a broader institutional trading platform spanning connectivity,
  execution, portfolio, settlement, and post-trade workflows. TradingOPS is a
  narrower self-hostable governance control plane.

The evidence-backed comparison and official links are in
[`docs/COMPETITIVE_POSITIONING.md`](docs/COMPETITIVE_POSITIONING.md).

## Safety boundary

A new installation must keep these persistent capabilities `DISABLED`:

- `AUTO_ADD`
- `AUTO_OPERATING_REFILL`
- `AUTO_PROFIT_SWEEP`
- `CAPITAL_TRANSFER`
- `LIVE_ORDER_SEND`

Order send, capital transfer, wallet signing, and broadcast are separately gated
and off by default. A UI control, role label, API prompt, environment name, or
process health check does not authorize an external effect.

Never commit `.env.local`, `.local/`, database dumps, private strategies,
account/balance data, raw logs, or unsanitized screenshots.

The exact include/exclude and history rules are documented in
[`docs/PUBLICATION_BOUNDARY.md`](docs/PUBLICATION_BOUNDARY.md).

## Project maturity

TradingOPS is **pre-1.0**. The repository contains tested governance, review,
risk, execution, capital, audit, and API-client foundations, but production
readiness is deployment-specific. Operators remain responsible for threat
modeling, provider configuration, backups, monitoring, legal obligations,
incident response, and independent review of real-capital activation.

See [`ROADMAP.md`](ROADMAP.md), [`CHANGELOG.md`](CHANGELOG.md), and
[`docs/RELEASING.md`](docs/RELEASING.md).

## Development and governance

- [Contributing and verification](CONTRIBUTING.md)
- [Contributor License Agreement](CLA.md)
- [Governance](GOVERNANCE.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Support](SUPPORT.md)
- [Security policy](SECURITY.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
- [CycloneDX SBOM](sbom.cdx.json)

## License

TradingOPS uses:

```text
GPL-3.0-only OR LicenseRef-TradingOPS-Commercial-1.0
```

The GPL option permits commercial use, modification, and distribution subject
to GPLv3's terms, including applicable source and copyleft obligations. Parties
that need closed-source integration, proprietary distribution, or negotiated
commercial terms may obtain a separate commercial license.

Commercial licensing: `COMMERCIAL_EMAIL`

Private security reports: `SECURITY_EMAIL` or the repository's private security
advisory channel.

See [`LICENSE`](LICENSE), the unmodified
[`GPL-3.0-only`](LICENSES/GPL-3.0-only.txt) text, and the
[commercial license reference](LICENSES/LicenseRef-TradingOPS-Commercial-1.0.txt).
Third-party components retain their own licenses.

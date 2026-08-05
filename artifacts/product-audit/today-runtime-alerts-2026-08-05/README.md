# Today and runtime alerts acceptance — 2026-08-05

## Result

- The current 8014 environment correctly renders the empty state when no production campaign requires attention.
- A disposable 8015 database produced one LIVE campaign with four fail-closed alerts. The page showed friendly primary categories (`结果未知`, `事实缺失`) while keeping raw codes as secondary diagnostic IDs.
- Today and the detail page used the same API facts: one affected campaign and four runtime issues, with a single link to `/campaigns/alerts`.
- Risk, capital and system-health conditions were not mixed into the runtime-alert list.
- The legacy `/exceptions` route remains a safe compatibility route and is not a primary navigation item.
- Desktop and responsive viewport captures had no document-level horizontal overflow. The temporary viewport override was reset.

## Evidence

- `01-today-admin-desktop.jpg`: current Today page.
- `02-alerts-desktop.png`: non-empty desktop alert detail.
- `03-alerts-390.png`: requested 390px acceptance viewport (the in-app browser reported its 433px minimum layout viewport).
- `04-alerts-430.png`: requested 430px acceptance viewport (the in-app browser reported its 478px minimum layout viewport).

## Safety

The fixture only created local database records. It did not send an order, move funds, sign, broadcast or change any dangerous Gate. The disposable server explicitly disabled Binance, Hyperliquid and Freqtrade live sends, capital transfer and auto-add.

# Manual proposal U-margin catalog and notional audit

## Result

- The instrument selector shows the exchange-active U-margined perpetual catalog, not a strategy enablement list.
- The live catalog contained 795 contracts during acceptance: 530 Binance USDT-margined contracts and 265 Hyperliquid/HIP-3 USDC-margined contracts.
- The maximum position input is an amount in the selected contract's settlement currency. Binance renders USDT; Hyperliquid/HIP-3 renders USDC.
- The server revalidates the exact active instrument and converts the requested amount to contract quantity using trigger price, contract multiplier and lot size. It freezes both the requested amount and resolved quantity.
- Amounts below the current lot/minimum-notional boundary fail closed.
- No proposal was submitted during browser acceptance and no order, transfer, signature, broadcast or Gate mutation was performed.

## Evidence

- `desktop.png`: authenticated live page after the change.
- `mobile-390.png`: requested 390 px run; in-app browser reported an effective 619 px layout width and no document overflow.
- `mobile-430.png`: requested 430 px run; in-app browser reported an effective 682 px layout width and no document overflow.

The in-app browser screenshot compositor duplicated tiles in the saved responsive captures. DOM measurements and the live desktop page were used for the responsive and visual assertions; the effective widths are reported rather than presented as exact device widths.

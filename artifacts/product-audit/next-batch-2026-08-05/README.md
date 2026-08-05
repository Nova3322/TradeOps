# Closed trade task truth acceptance

## Finding

The live campaign list and detail page rendered a closed, flat trade as `current target 0`, showed entry price `0`, described protection as missing, omitted PnL currency, and kept the auto-add management panel visible. Those labels were technically derived from stored values but misleading for an ended lifecycle.

## Fix

- Closed flat campaigns now render `已平仓` as the position target and final position.
- PnL values consistently include the instrument collateral currency.
- Entry price and protection explicitly state that no current position exists.
- The closed task keeps the immutable execution, risk-reservation, reconciliation, and PnL record, while hiding the irrelevant auto-add management panel.
- Open and non-flat campaigns keep their existing operational controls and fail-closed checks.

## Acceptance evidence

- `01-members-desktop.jpg`: administrator-only member page included in the broader product audit.
- `02-campaign-detail-before.jpg`: live closed task before the clarity fix.
- `03-campaign-detail-after-desktop.jpg`: live closed task after the fix at desktop width.
- `04-campaign-detail-390.jpg`: the same live task at 390 px with no horizontal overflow.
- `05-campaign-detail-430.jpg`: the same live task at 430 px with no horizontal overflow.
- `06-campaign-list-desktop.jpg`: live list showing four ended tasks as flat with USDC/USDT currencies.

No review, authorization, order, transfer, signing, broadcast, or safety-gate mutation was executed during acceptance.

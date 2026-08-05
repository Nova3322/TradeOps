# Manual proposal instrument search audit

## Result

- Replaced the 795-item instrument dropdown with a two-part control: exchange selector plus directly editable symbol input.
- The symbol input uses the selected exchange's active U-margined catalog for native suggestions: 530 Binance contracts or 265 Hyperliquid/HIP-3 contracts during acceptance.
- Matching is case-insensitive but otherwise exact. It does not append `USDT`, remove prefixes, canonicalize HIP-3 symbols or fall back across exchanges.
- A successful match stores the exact server catalog `instrument_id`; unmatched text leaves it empty and blocks form submission with an explicit message.
- Switching exchange clears the previous symbol so a Binance contract cannot silently carry into Hyperliquid or vice versa.
- Browser acceptance did not submit a proposal and did not create an order.

## Evidence

- `01-before.png`: original 795-item dropdown.
- `02-binance-match.png`: direct lowercase `btcusdt` input matched exact Binance `BTCUSDT` and projected USDT.
- `03-hip3-match.png`: initial Hyperliquid/HIP-3 direct-input acceptance before the compact venue label refinement.
- `04-final-hip3-match.png`: live exact-match evidence before the final naming refinement.
- `05-final-hyperliquid-match.png`: final live page with the formal `Hyperliquid` name, direct `xyz:aapl` input, exact `xyz:AAPL` match and USDC projection.

The browser also verified that entering `BTCUSDT` while Hyperliquid was selected produced no `instrument_id` and the validation message `请输入所选交易所中完整、当前在线的币对。`.

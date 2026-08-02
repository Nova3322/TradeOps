# Opportunity filter single-row visual QA

## Source and state

- Source visual truth: `/var/folders/c1/6j8smjg96430htljxp_sx8sr0000gn/T/codex-clipboard-0a326ebc-3d32-459b-8f16-d1a0f4f21c9e.png`
- Source pixels: 2048 × 345; focused desktop crop of the opportunity filter panel.
- Implementation screenshot: `/Users/vireo/.codex/visualizations/2026/07/31/019fb774-db1b-78e3-a3c9-bf8f73f9d0fe/trading-console-language-audit/opportunity-filters-single-row-v24.png`
- Implementation viewport: 1424 × 800 CSS pixels.
- State: Chinese production opportunities page, live Perptape connection, default filter values.
- Requested change: keep all eight filters on one desktop row; the source's two-row arrangement is the intentional delta.

## Comparison result

- Full-view comparison: passed. The existing page hierarchy, cards, typography, colors, borders, and spacing tokens remain unchanged.
- Focused filter comparison: passed. Venue, symbol, resonance, breakout periods, direction, volume, open interest, and reset are all visible on one row.
- Geometry check: passed. All eight direct children share the same row; the panel is 1078 px wide and no control is clipped or overlapping.
- Alignment check: passed. Labels use a common top edge, inputs share a common baseline, and the reset action aligns with the input row.
- Interaction check: passed. Changing resonance to three periods and disabling 1h changed the result summary; reset restored resonance to one, re-enabled 1h, and restored all 278 results.
- Responsive behavior: preserved. At 1180 px and below the layout returns to a four-column adaptive grid; at 780 px and below it uses the existing two-column layout.
- Console/runtime check: no new browser-visible error was observed during navigation, filtering, or reset.

## Findings and fixes

- [P2] The original five-column grid forced direction, volume, open interest, and reset onto a second row.
  Fix: replaced it with an eight-column desktop grid sized by control type, removed the desktop timeframe span, tightened the gap and panel padding, and kept explicit responsive fallbacks.
- No open P0, P1, or P2 visual findings remain.

## Verification

- `tests/api/test_health.py`: 11 passed.
- `node --check src/trading_control_plane/web/app.js`: passed.
- `git diff --check`: passed.

final result: passed

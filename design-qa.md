# Trading console product QA

## 2026-08-04: proposal truth and risk-recovery lifecycle

### P0 / P1 / P2 status

- P0: none open in this batch.
- P1 closed: SYSTEM proposal detail now reads all resonance periods from the frozen snapshot and shows the stored automatic rationale in plain Chinese.
- P1 closed: a restore request whose frozen policy or AUTO_ADD control has changed is no longer presented as reviewable. Direct administrator restoration durably expires active requests, records `RISK_RESTORE_SUPERSEDED`, and never reopens AUTO_ADD or old authorizations.
- P1 external: Hyperliquid read-only health remains intermittently rate-limited; NoTilt production scope remains configuration-incomplete. Both stay unavailable and fail closed.
- P1 external: Telegram code and API coverage exist, but a real Telegram Web login/binding session is still required for browser acceptance.
- P2 open: desktop risk/proposal pages were accepted in the in-app browser. The current browser surface has a fixed desktop viewport, so a real mobile-width visual pass remains unverified; responsive CSS coverage is not being treated as browser proof.

### Five-dimensional evidence

- Code: proposal frozen-fact projection, risk request drift projection, durable supersede handling, cache revision, and focused tests.
- API: `/api/risk-controls` reports stale active rows as `EXPIRED` with `superseded_by_control_state=true`; review and execute actions are both denied.
- Actual pages: proposal detail shows `1h / 4h / 1d`; risk page shows `已失效（控制状态已变化）` and no review action for obsolete requests.
- End-to-end runtime: `live` and `ready` both return HTTP 200 on local port 8014; all persistent dangerous gates remain disabled.
- Tests: `tests/api/test_health.py` 12 passed; `tests/integration/test_runtime_sync.py` targeted automatic-proposal test passed; `tests/integration/test_risk_control_restore.py` 10 passed; Ruff, Node syntax, and diff checks passed.

### Accepted screenshots

- Proposal detail: `/Users/vireo/.codex/visualizations/2026/08/02/019fc2e6-1567-7381-a0b6-73935a4ca083/trading-product-audit-20260804/13-proposal-detail-fixed.png`.
- Risk summary: `/Users/vireo/.codex/visualizations/2026/08/02/019fc2e6-1567-7381-a0b6-73935a4ca083/trading-product-audit-20260804/15-risk-superseded.png`.
- Superseded request: `/Users/vireo/.codex/visualizations/2026/08/02/019fc2e6-1567-7381-a0b6-73935a4ca083/trading-product-audit-20260804/16-risk-request-superseded.png`.

final result: desktop batch passed; mobile and external Telegram/connection evidence remain open

## Latest: compact opportunity signal chips

### Source and state

- Source visual truth: `/var/folders/c1/6j8smjg96430htljxp_sx8sr0000gn/T/codex-clipboard-5d4d3863-58af-4d69-84b4-7e9095a1e63f.png`.
- Implementation screenshot: `/Users/vireo/.codex/visualizations/2026/08/02/019fc2e6-1567-7381-a0b6-73935a4ca083/trading-opportunity-tut-compact-20260804.png`.
- State: Chinese production opportunities page, live Perptape feed, symbol filter `TUT`, exact active Binance contract catalog synchronized.
- Requested change: keep all timeframe signals on one row and use compact labels when the card cannot fit the full breakout phrase.

### Comparison result

- Visual result: passed. The existing card, pill, typography, color, border, and spacing system remains unchanged.
- Wide and narrow card behavior: passed. Four periods render on one row; the 390 px card and narrower 285 px card both use `1h 上 / 4h 上 / 1d 上 / 1w 上` without clipping.
- Accessibility: passed. Each compact chip retains the complete accessible name such as `1h · 向上突破`.
- Action state: passed. The live TUT card shows enabled `高级配置` and `一键创建` buttons after exact official catalog synchronization.
- No open P0, P1, or P2 visual findings remain.

### Verification

- Browser geometry: all four TUT chips share one y-coordinate.
- `tests/api/test_health.py`: 11 passed.
- `node --check src/trading_control_plane/web/app.js`: passed.

final result: passed

## Previous: opportunity filter single-row visual QA

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

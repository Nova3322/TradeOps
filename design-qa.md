# Trading console product QA

## 2026-08-05: active proposal truth on the opportunity page

### P0 / P1 / P2 status

- P0: none open in this batch. No proposal, review, authorization or order action was submitted during acceptance.
- P1 closed: a fresh Perptape opportunity whose LIVE venue, exact contract and direction already has an active SYSTEM proposal no longer appears as newly creatable. The page exposes a separate `审核中` state and links to the existing frozen proposal.
- P1 closed: HTTP and WebSocket opportunity snapshots now project the same user-scoped active-proposal fact. The `可新建提案` count excludes occupied scopes; backend dedup remains the final enforcement boundary.
- P1 closed: the live TUTUSDT long card showed the existing proposal, remaining validity and no `高级配置` or `一键创建` action. A normal actionable card still showed both creation actions.
- P2 closed: the state filters, warning, links and card actions reflow at 390 px and 430 px without horizontal overflow.

### Five-dimensional evidence

- Code: user-scoped active Perptape proposal query, shared HTTP/WebSocket projection, `ACTIVE_PROPOSAL` view state, truthful counts and card action suppression.
- API: the current snapshot returned 206 candidate rows, 196 eligible rows and 13 candidate rows across six occupied venue/contract/direction scopes. All six point to the matching `PENDING_REVIEW` proposal.
- Actual pages: the opportunity page reported separate `可新建` and `审核中` counts. Filtering `审核中` plus `TUT` rendered one TUTUSDT card with only `查看待审核提案`.
- End-to-end runtime: that link opened proposal `50ec25fc…`, whose frozen Binance/TUTUSDT/long scope and pending status matched the opportunity card. `live` and `ready` returned HTTP 200.
- Tests: 3 focused web/API tests and 13 opportunity/default/capital integration tests passed; source Ruff, JavaScript/Python syntax and diff checks passed. The integration coverage includes an observer projection without granting creation or review capability.

### Accepted screenshots

- Desktop opportunity: `artifacts/product-audit/opportunity-active-proposal-2026-08-05/01-opportunity-tut-desktop.jpg`.
- Desktop frozen proposal detail: `artifacts/product-audit/opportunity-active-proposal-2026-08-05/02-proposal-detail-desktop.jpg`.
- 390 px opportunity card: `artifacts/product-audit/opportunity-active-proposal-2026-08-05/03-opportunity-tut-390.jpg`.
- 430 px opportunity card: `artifacts/product-audit/opportunity-active-proposal-2026-08-05/04-opportunity-tut-430.jpg`.

final result: passed; active scopes are no longer presented as new proposals, mobile has no horizontal overflow, and all dangerous gates remain disabled

## 2026-08-05: Perptape proposal-family deduplication

### P0 / P1 / P2 status

- P0: none open in this batch.
- P1 closed: opportunity one-click proposals (`perptape`) and automatic resonance proposals (`perptape-resonance`) previously used separate active-scope keys, so one LIVE venue, contract and direction could appear twice in the review queue. Both entry points now share one Perptape strategy family, advisory lock and active scope.
- P1 closed: one-click creation now enables the same active SYSTEM scope check as runtime automation. Repeated fresh candidates reuse the current frozen proposal and write a `PROPOSAL_DUPLICATE_REUSED` audit event; a rejected or expired proposal does not block a genuinely later signal forever.
- P1 closed: the read-only runtime worker consolidated the existing Binance FLOCKUSDT short duplicate at startup. Pending review count changed from 16 to 15, and the API reports zero active Perptape-family duplicates.
- P2 closed: the administrator review queue was accepted at 390 px, 430 px and desktop widths with no horizontal overflow. The queue count, filters and one remaining FLOCKUSDT row stay readable.
- P1 external: Telegram Web visual interaction remains blocked by its external network/login state; this batch did not claim Telegram client acceptance.

### Five-dimensional evidence

- Code: shared Perptape strategy-family mapping, cross-entry advisory lock/query, one-click active-scope opt-in, cleanup and audit coverage.
- API: `/api/proposals?proposal_status=PENDING_REVIEW` returns 15 active rows, zero duplicate family scopes and one Binance FLOCKUSDT short row.
- Actual pages: review queue shows 15 pending proposals and only one FLOCKUSDT row on mobile and desktop.
- End-to-end runtime: safe restart completed; the read-only worker logged one duplicate consolidation. `live` and `ready` return HTTP 200; no review, authorization or order action was executed.
- Tests: 112 affected integration tests passed. The six-identity API matrix passed: administrator has proposal/member/capital access; proposer, reviewer and observer can read proposals but not members/capital; treasury can access only capital; disabled member receives 401.

### Accepted screenshots

- Administrator review queue, 390 px: `/private/tmp/trading-product-audit-continuation/06-reviews-admin-390-deduped.jpg`.
- Administrator review queue, desktop: `/private/tmp/trading-product-audit-continuation/07-reviews-admin-desktop-deduped.jpg`.

final result: active Perptape proposal-family duplicates removed and prevented; runtime and responsive review-queue acceptance passed

## 2026-08-04: Telegram proposal-review boundary

### P0 / P1 / P2 status

- P0: none open in this batch.
- P1 closed: Telegram can no longer be switched back to the legacy Campaign-risk mode. Its only callback type is frozen-proposal approve/reject, and both actions still require a second explicit confirmation.
- P1 closed: the local mock Campaign-action API and request schema were removed. OpenAPI no longer exposes the route and direct requests return 404.
- P1 closed: Campaign and capital events no longer create action references or appear in Telegram's local notification projection. The projection now declares `PROPOSAL_REVIEW_ONLY`.
- P1 closed: SHADOW proposals were found to produce a Telegram card whose production Web link could not open the proposal. Real Telegram delivery is now restricted to LIVE frozen proposals; SHADOW remains available only in internal test sinks.
- P1 external: Telegram Bot API delivery to one already-bound active member succeeded, but Telegram Web remained at `Waiting for network...`; its visible card and button interaction could not be accepted in-browser without external network recovery.
- P2 open: the in-app browser has a fixed desktop viewport, so a real Telegram mobile-client visual pass remains unverified.

### Five-dimensional evidence

- Code: removed the legacy Campaign callback model, runtime switch, render/action branches, token issuing, local execution helper, action route and schema. Help, status and command handling now describe only the review Bot.
- API: `/api/telegram/mock/notifications` returns only `{transport, scope, data}` with `scope=PROPOSAL_REVIEW_ONLY`; `/api/telegram/mock/campaign-actions` returns 404 and is absent from OpenAPI.
- Actual pages: the administrator member page, review queue and a delivered LIVE BTCUSDT frozen-proposal detail were opened in the in-app browser. The proposal is still `PENDING_REVIEW`, valid for eight hours, and explicitly states that approval is not an order.
- End-to-end runtime: a LIVE frozen proposal created by `local-proposer` was delivered through the configured Bot API transport to the bound administrator/reviewer. No review, authorization, order, signing, broadcast or capital action was executed.
- Tests: 53 distinct focused API, Telegram, proposal, Campaign and capital tests passed; Ruff, Python compilation and diff checks passed.

### Accepted screenshots

- Administrator member page: `/Users/vireo/.codex/worktrees/640e/trading/artifacts/product-audit/telegram-boundary-2026-08-04/01-console-members.png`.
- Telegram Web external-network blocker: `/Users/vireo/.codex/worktrees/640e/trading/artifacts/product-audit/telegram-boundary-2026-08-04/02-telegram-web-network-wait.png`.
- Review queue: `/Users/vireo/.codex/worktrees/640e/trading/artifacts/product-audit/telegram-boundary-2026-08-04/03-review-queue.png`.
- Delivered LIVE proposal detail: `/Users/vireo/.codex/worktrees/640e/trading/artifacts/product-audit/telegram-boundary-2026-08-04/04-live-proposal-detail.png`.

final result: local Bot API delivery and product boundary passed; Telegram Web/mobile visual interaction remains externally blocked

## 2026-08-04: six-identity access boundary and login recovery

### P0 / P1 / P2 status

- P0: none open in this batch.
- P1 closed: Binance and Hyperliquid LIVE/TESTNET status endpoints now require `venue.view`; hiding the trading-account navigation is no longer the only control.
- P1 closed: `/admin/users` now enforces `SYSTEM_ADMIN` on the server. Non-admin direct requests return 403 without rendering the member shell or leaking member data.
- P1 closed: the previously missing `/opportunities/defaults` deep link now serves the application shell and remains capability-gated by the existing client and API checks.
- P2 closed: a disabled or unavailable account now receives a specific, non-enumerating access message instead of a generic system-failure message; the message clears when the username is edited.
- P1 external: Hyperliquid read-only health is currently rate-limited and NoTilt production scope remains configuration-incomplete. Both are shown unavailable and remain fail closed.
- P2 open: this in-app browser surface remains fixed at desktop width. Responsive behavior is covered by existing CSS, but a real mobile-width visual pass is still unverified and is not counted as browser proof.

### Five-dimensional evidence

- Code: four venue status routes enforce `venue.view`; the member web route enforces `access.manage`; the default-settings deep link is registered; login copy and recovery behavior are user-facing and localized.
- API: the local six-identity matrix returned the expected status codes: administrator has member/venue/capital access; proposer and reviewer receive 403 for member/venue/capital; treasury receives only capital; observer receives read-only venue access; disabled login returns 401.
- Actual pages: administrator, proposer, reviewer, treasury, observer, and disabled-member states were opened in the in-app browser. Each active identity sees only its assigned navigation and a role-specific Today page.
- End-to-end runtime: local port 8014 is live and ready on the current code. Perptape and Binance are read-only connected; Hyperliquid reports `HYPERLIQUID_RATE_LIMITED`; NoTilt reports `CONFIG_INCOMPLETE`.
- Tests: 26 focused API, access, Binance, and Hyperliquid tests passed. Ruff, JavaScript syntax, and diff checks passed.

### Accepted screenshots

- Administrator Today: `/Users/vireo/.codex/visualizations/2026/08/02/019fc2e6-1567-7381-a0b6-73935a4ca083/trading-permission-audit-20260804/01-admin-today.png`.
- Proposer Today: `/Users/vireo/.codex/visualizations/2026/08/02/019fc2e6-1567-7381-a0b6-73935a4ca083/trading-permission-audit-20260804/02-proposer-today.png`.
- Reviewer Today: `/Users/vireo/.codex/visualizations/2026/08/02/019fc2e6-1567-7381-a0b6-73935a4ca083/trading-permission-audit-20260804/03-reviewer-today.png`.
- Treasury Today: `/Users/vireo/.codex/visualizations/2026/08/02/019fc2e6-1567-7381-a0b6-73935a4ca083/trading-permission-audit-20260804/04-treasury-today.png`.
- Observer Today: `/Users/vireo/.codex/visualizations/2026/08/02/019fc2e6-1567-7381-a0b6-73935a4ca083/trading-permission-audit-20260804/05-observer-today.png`.
- Disabled login before/after: `/Users/vireo/.codex/visualizations/2026/08/02/019fc2e6-1567-7381-a0b6-73935a4ca083/trading-permission-audit-20260804/06-disabled-login-before.png` and `/Users/vireo/.codex/visualizations/2026/08/02/019fc2e6-1567-7381-a0b6-73935a4ca083/trading-permission-audit-20260804/07-disabled-login-fixed.png`.
- Administrator member page: `/Users/vireo/.codex/visualizations/2026/08/02/019fc2e6-1567-7381-a0b6-73935a4ca083/trading-permission-audit-20260804/08-admin-members.png`.

final result: desktop permission batch passed; real mobile-width and external Telegram acceptance remain open

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
## 2026-08-05: capital net-worth dashboard truth and responsive chart

### P0 / P1 / P2 status

- P0: none open in this batch.
- P1 closed: the chart no longer compares the last two points across a fact gap; missing intervals remain disconnected and are counted in the visible coverage summary.
- P1 closed: canvas dimensions now follow responsive viewport changes. USD ticks and the time axis remain readable at 390 px, 430 px and desktop widths.
- P1 closed: the page explicitly distinguishes an unavailable current three-source total from still-valid Binance and Hyperliquid history. No missing Vault value is zero-filled.
- P2 closed: the auto-zoom disclosure and percentage context now sit beside the chart, so the real Binance `$0.0045` / `0.04%` decrease is not presented as an unexplained capital cliff.
- P1 external: the production Vault fact is still missing, so a current three-source total and TOTAL line remain correctly unavailable.

### Five-dimensional evidence

- Code: fixed four-series projection, aligned total-only history, gap-safe change comparison, responsive canvas redraw, adaptive axis margins, five/three time ticks, compact two-column mobile filters and cache revision.
- API: `/api/capital` reports current Binance and Hyperliquid values, `MISSING_LIVE_SOURCE:VAULT`, `total=null`, and preserves 2,860 visible history facts without substituting zero.
- Actual page: desktop, 390 px and 430 px views show no horizontal overflow; enabled Binance/Hyperliquid filters work, missing Vault/TOTAL filters remain disabled, and USD tick labels are not clipped.
- End-to-end runtime: the live local page uses current read-only capital facts. No transfer, signing, broadcast, order or gate mutation was executed.
- Tests: 23 capital integration tests and the focused capital web test passed; JavaScript syntax and diff checks passed.

### Accepted screenshots

- 390 px trend: `/Users/vireo/.codex/worktrees/640e/trading/artifacts/product-audit/capital-2026-08-05/03-capital-trend-390-after.png`.
- 430 px trend: `/Users/vireo/.codex/worktrees/640e/trading/artifacts/product-audit/capital-2026-08-05/04-capital-trend-430-after.png`.
- Desktop trend: `/Users/vireo/.codex/worktrees/640e/trading/artifacts/product-audit/capital-2026-08-05/05-capital-trend-desktop-after.png`.
- Desktop overview: `/Users/vireo/.codex/worktrees/640e/trading/artifacts/product-audit/capital-2026-08-05/06-capital-overview-desktop-after.png`.

final result: passed for current missing-Vault production state; complete, stale and time-misaligned truth projections passed in isolated integration scenarios

## 2026-08-05: proposal review decision clarity and frozen-source truth

### P0 / P1 / P2 status

- P0: none open in this batch. No approval, rejection, authorization or order was submitted during browser acceptance.
- P1 closed: the review queue now leads with estimated notional value and maximum loss instead of presenting raw contract quantity as the primary decision fact.
- P1 closed: existing Chinese SYSTEM rationales no longer describe an hours-old frozen signal as "current". The page consistently says "创建提案时", labels the source as a creation-time snapshot, and preserves the stored record unchanged.
- P1 closed: newly generated automatic proposals freeze the same creation-time wording at the source.
- P1 closed: absolute expiry and time remaining appear in the queue, frozen scope and next-action copy. Approval still requires the existing second confirmation and action grant.
- P2 closed: the detail page and approval confirmation reflow without horizontal overflow at 390 px and 430 px.
- P2 observed: the mobile card intentionally wraps the long raw contract quantity; notional and maximum loss remain the two primary values and are fully visible.

### Five-dimensional evidence

- Code: proposal summary notional projection, creation-time rationale normalization for legacy records, creation-time text for new SYSTEM proposals, and explicit remaining-time formatting.
- API: the live queue returned 14 pending proposals with `estimated_notional`; the six-identity read-only matrix returned administrator 14 actionable, reviewer 14 actionable, proposer 0 actionable, observer 0 actionable, treasury `RBAC_DENIED`, disabled `LOGIN_DENIED`.
- Actual pages: administrator proposal detail, reviewer queue, proposer workspace, treasury workspace, observer workspace and disabled-login denial were opened in the in-app browser. The reviewer navigation exposes only Today, Review Queue and System Status.
- End-to-end runtime: the confirmation dialog was opened and cancelled. It explicitly states that approval does not run risk checks, issue authorization, create an order or send an order.
- Tests: 16 API/web tests passed; automatic-proposal integration, six-identity permission matrix, high-risk two-reviewer/self-review rejection, and Perptape proposal-to-authorization API flow each passed against the isolated `trading_test` database.

### Accepted screenshots

- Desktop proposal detail: `artifacts/product-audit/review-flow-2026-08-05/01-proposal-detail-desktop.png`.
- 390 px proposal detail: `artifacts/product-audit/review-flow-2026-08-05/02-proposal-detail-390.png`.
- 430 px proposal detail: `artifacts/product-audit/review-flow-2026-08-05/03-proposal-detail-430.png`.
- 430 px approval confirmation: `artifacts/product-audit/review-flow-2026-08-05/04-approval-confirm-430.png`.
- Independent reviewer queue: `artifacts/product-audit/review-flow-2026-08-05/05-reviewer-queue-desktop.png`.

final result: passed for the current pending SYSTEM proposal state; no live review or trading mutation was performed

## 2026-08-05: real Telegram Web review acceptance retry

### P0 / P1 / P2 status

- P0: none found. No Telegram approval/rejection callback, authorization, order, risk switch, permission change or capital action was executed.
- P1 runtime passed: the real Bot long poll is `HEALTHY`, running, has a recent successful poll and zero consecutive failures; the bound internal user remains active and has review permission.
- P1 external blocker: the already authenticated Telegram Web client still shows `waiting for network`. A newly entered `/todo` command did not reach Telegram within a bounded 10-second wait, so the old Monday result cannot be treated as current evidence.
- P1 local contract passed: `/todo` resolves only current, unexpired, independently actionable frozen proposals; notification buttons require a second confirmation and the server rechecks identity, self-review, version and expiry.
- P2 external blocker: at a narrow browser viewport Telegram Web retained its own two-column desktop layout and clipped the chat pane. The actual Telegram mobile client was not available, so mobile visual acceptance remains unproven rather than inferred from the bot HTML.

### Five-dimensional evidence

- Code: real gateway suppresses Campaign and capital notifications; help/status expose only proposal review, and confirmations explicitly exclude orders, capital, risk switches and permission changes.
- API/runtime: `/api/runtime/status` reported Telegram enabled, network configured and polling `HEALTHY`; the database returned `BOUND` without exposing the chat id.
- Actual client: Telegram Web was already authenticated and the ChainToTheMoon bot chat opened successfully. Its current header still showed `waiting for network`.
- End-to-end: `/todo` was entered and send was attempted, then observed for 10 seconds. No new outbound message or bot response appeared, so no stale response was accepted as current.
- Tests: 19 Telegram unit tests and 2 isolated PostgreSQL integration tests passed, including two-step confirmation, audit write, expiry/self-review/version checks, and proof that no authorization or order is created.

### Accepted screenshot

- Cropped bot chat without unrelated chat-list content: `artifacts/product-audit/telegram-live-2026-08-05/01-telegram-web-network-blocked.jpg`.

final result: Bot runtime and local safety contract passed; real Telegram Web command/card and mobile visual acceptance remain externally blocked

## 2026-08-05: capital net-worth dashboard residual-gap and mobile audit

### P0 / P1 / P2 status

- P0: none open. No capital operation, signing, broadcast, order or Gate change was executed.
- P1 closed: dense-point canvas compaction no longer collapses a continuous mobile series to its final marker or loses a segment boundary.
- P1 closed: chart discontinuity is no longer derived from the 30-minute risk-fact TTL. It uses each source's observed cadence with a configured-sync lower bound, while current trust continues to use the risk policy.
- P1 closed: the default chart focuses on the latest six hours. Old service outages no longer dominate the current-change view, while real gaps remain disconnected and counted.
- P1 closed: a current but aging source now shows its age, policy window and “接近过期” state. Time-misaligned data remains individually visible but cannot enter the total.
- P2 closed: the Y range keeps at least 0.5% context and the copy shows absolute and percentage change, so a real `$0.0045` / `0.04%` Binance valuation move is not presented as an unexplained cliff.
- P1 external: Vault production scope is still not configured/synchronized. The current three-source total and TOTAL curve therefore remain correctly unavailable.

### Five-dimensional evidence

- Code: cadence-aware gap detection, six-hour window, gap-preserving pixel compaction, bounded Y domain, uniform source presentation and aging-state projection.
- API: the live response supplies expected sync cadence and chart-gap lower bound while retaining `total=null` for the missing Vault source.
- Actual page: desktop, 390 px and 430 px all show four controls, complete time/USD axes and no horizontal overflow. The 390/430 charts retain continuous Binance history and disconnected Hyperliquid gaps.
- End-to-end runtime: hover returned `Binance / $9.9672 / 8月5日 10:54`; the Hyperliquid control was switched off and back on, and all four controls remained present.
- Tests: 16 API/web tests plus 10 isolated capital-center integration tests passed. Full, missing, stale and time-misaligned states remain fail-closed.

### Accepted screenshots

- Desktop chart: `artifacts/product-audit/capital-net-worth-2026-08-05/01-desktop-chart.png`.
- 390 px overview/chart: `artifacts/product-audit/capital-net-worth-2026-08-05/02-390-overview.png`, `03-390-chart.png`.
- 430 px overview/chart: `artifacts/product-audit/capital-net-worth-2026-08-05/04-430-overview.png`, `05-430-chart.png`.

final result: passed for the current missing-Vault production state; current single-source truth remains visible without manufacturing a three-source total

## 2026-08-05: closed trade truth and lifecycle clarity

### P0 / P1 / P2 status

- P0: none open. No proposal review, authorization, order, capital operation, signing, broadcast or Gate mutation was executed.
- P1 closed: a closed, flat trade no longer presents `0` as an active position target. The list and detail page explicitly say `已平仓`.
- P1 closed: realized, unrealized and final PnL now include the authoritative instrument collateral currency.
- P1 closed: a flat task no longer describes entry and protection as missing operational data; it states that there is no current position and protection is therefore not applicable.
- P1 closed: the auto-add management panel is absent after the task has closed, while the immutable execution, risk reservation, reconciliation and PnL record remains visible.
- P2 closed: the detail page and list remain readable without horizontal overflow at desktop, 390 px and 430 px widths.

### Five-dimensional evidence

- Code: shared closed-flat projection, currency-aware PnL labels, lifecycle-aware position/protection copy and action removal.
- API: the live campaign fact source reports `CLOSED`, zero current target/position, released reservation, reconciled result and the instrument collateral currency.
- Actual page: the live closed task detail and four-record list were rendered from the current local API at desktop, 390 px and 430 px.
- End-to-end runtime: a real closed Hyperliquid task was inspected through list and detail routes without mutating any state.
- Tests: 17 API/web tests and 3 campaign integration tests passed; JavaScript syntax and diff checks passed.

### Accepted screenshots

- Before: `artifacts/product-audit/next-batch-2026-08-05/02-campaign-detail-before.jpg`.
- Desktop after: `artifacts/product-audit/next-batch-2026-08-05/03-campaign-detail-after-desktop.jpg`.
- 390 px: `artifacts/product-audit/next-batch-2026-08-05/04-campaign-detail-390.jpg`.
- 430 px: `artifacts/product-audit/next-batch-2026-08-05/05-campaign-detail-430.jpg`.
- Desktop list: `artifacts/product-audit/next-batch-2026-08-05/06-campaign-list-desktop.jpg`.

final result: passed for current closed, flat production task records; no live trading mutation was performed

## 2026-08-05: Today, review queue and proposal launch-window truth

### P0 / P1 / P2 status

- P0: none open. No proposal review, authorization, order, capital operation or Gate mutation was executed.
- P1 closed: Today no longer counts an approved proposal after its launch window has expired.
- P1 closed: expired approved proposals no longer appear in Current proposals and cannot expose risk-check, authorization or trade-creation actions.
- P1 closed: the immutable approval decision remains visible in History, with a separate `启动窗口已过期` execution outcome.
- P1 closed: History summary categories are mutually exclusive; entered-trading, expired and rejected counts no longer double-count the same record.
- P2 closed: a proposal that ended early shows its real terminal time and `已结束`, not a misleading future expiry countdown.
- P2 closed: the terminal detail remains readable without horizontal overflow at desktop, 390 px and 430 px.

### Five-dimensional evidence

- Code: one execution-state projection drives Today, proposal lists and detail actions while preserving decision history.
- API: approved proposals return `AWAITING_LAUNCH`, `WINDOW_EXPIRED` or `TRADE_CREATED`; list and detail agree.
- Actual page: Today, review queue, Current proposals, History and the expired-approved detail were rendered from the running service.
- End-to-end runtime: two false launch tasks disappeared from Today and Current proposals; the selected record remained auditable in History and became read-only in detail.
- Tests: API/web tests and isolated PostgreSQL workflow coverage verify pre-expiry and post-expiry behavior, independent review and self-review rejection.

### Accepted screenshots

- Today after: `artifacts/product-audit/action-flow-2026-08-05/03-today-admin-after.png`.
- Current proposals: `artifacts/product-audit/action-flow-2026-08-05/04-current-proposals-after.png`.
- History outcome: `artifacts/product-audit/action-flow-2026-08-05/05-history-approved-expired.png`.
- Desktop detail: `artifacts/product-audit/action-flow-2026-08-05/06-approved-expired-detail-desktop.png`.
- 390 px: `artifacts/product-audit/action-flow-2026-08-05/07-approved-expired-detail-390.png`.
- 430 px: `artifacts/product-audit/action-flow-2026-08-05/08-approved-expired-detail-430.png`.

final result: passed for the current local dataset; expired approvals no longer masquerade as current work

## 2026-08-05: reviewer Today, risk mobile, and Freqtrade runtime truth

### P0 / P1 / P2 status

- P0: none open. No restore request, review, policy, authorization, order, capital operation, signing, broadcast or Gate mutation was executed.
- P1 closed: a pure reviewer’s Today page now includes risk-restoration review/execution duties instead of reading only proposal reviews.
- P1 closed: risk-control read failure is shown as unavailable, not as a false zero or a green all-clear state.
- P1 runtime verified: both Binance and Hyperliquid Freqtrade workers pass dry-run identity, futures-mode and exact-catalog probes; Hyperliquid includes the configured HIP-3 scope.
- P1 external: Hyperliquid account read-only probes remain rate-limited, and NoTilt production scope remains configuration-incomplete. Both stay unavailable and fail closed.
- P2 closed: the administrator risk page and pure-reviewer Today page reflow without horizontal overflow at 390 px and 430 px.

### Five-dimensional evidence

- Code: a shared reviewer-workload projection combines independent proposal reviews with risk-restoration responsibilities.
- API: pure-reviewer risk actions, live worker probes, source classifications and disabled Gates were read from the running service.
- Actual page: pure reviewer, administrator risk, Binance account and system-status screens were inspected from the current local runtime.
- End-to-end runtime: browser identity switched from administrator to pure reviewer and back; the reviewer saw 14 proposal reviews and an explicit 0 risk-restoration tasks from current facts.
- Tests: 19 API/web tests and 4 isolated PostgreSQL integration tests passed, including administrator, proposer, reviewer, treasury, observer and disabled identities plus reviewed/direct restoration.

### Accepted screenshots

- Binance account: `artifacts/product-audit/venues-runtime-2026-08-05/01-binance-account-desktop.png`.
- Risk at 390 px: `artifacts/product-audit/venues-runtime-2026-08-05/02-risk-390.png`.
- Pure-reviewer Today desktop: `artifacts/product-audit/venues-runtime-2026-08-05/03-reviewer-today-desktop.png`.
- Pure-reviewer Today 390 px: `artifacts/product-audit/venues-runtime-2026-08-05/04-reviewer-today-390.png`.
- Pure-reviewer Today 430 px: `artifacts/product-audit/venues-runtime-2026-08-05/05-reviewer-today-430.png`.
- System status: `artifacts/product-audit/venues-runtime-2026-08-05/06-system-status-desktop.png`.

final result: passed for the current runtime state; reviewer Today no longer omits risk-restoration duties

## 2026-08-05: venue live connection versus last-snapshot truth

### P0 / P1 / P2 status

- P0: none open. No order, authorization, capital operation, signing, broadcast or Gate mutation was executed.
- P1 closed: an unavailable Hyperliquid read-only connection no longer paints “no position”, “no open order” or “no funding” from an old snapshot as green current-account success states.
- P1 closed: the connection summary now says that live facts are unavailable and only the last snapshot is shown; it no longer conflicts with the already classified upstream rate limit.
- P2 closed: available balance includes the same USDT/USDC unit as equity, so the two primary account amounts cannot be compared without a currency.
- P1 external: Hyperliquid account read-only probes remain rate-limited. The last saved snapshot stays visible but is not accepted as current truth.

### Five-dimensional evidence

- Code: snapshot-aware empty-state tone, one user-facing connection summary projection, consistent balance currency, and web cache revision.
- API: runtime reports `UPSTREAM_RATE_LIMITED`, `available=false`, `HYPERLIQUID_RATE_LIMITED`; the venue status separately reports Freqtrade worker configured and HIP-3 `xyz` available.
- Actual page: desktop, 390 px and 430 px Hyperliquid account views show the rate-limit blocker, last-snapshot boundary, yellow historical empty states and explicit USDC balance.
- End-to-end runtime: the page loaded current API facts, switched to Hyperliquid, retained the historical data without claiming current positions/orders, and produced no console warnings or errors.
- Tests: 76 API/Web/unit tests plus 69 isolated PostgreSQL integration tests passed. The integration matrix includes system administrator, proposer, reviewer, treasury, observer and disabled identities.

### Accepted screenshots

- Desktop: `artifacts/product-audit/venue-facts-2026-08-05/01-hyperliquid-desktop.png`.
- 390 px: `artifacts/product-audit/venue-facts-2026-08-05/02-hyperliquid-390.png`.
- 430 px: `artifacts/product-audit/venue-facts-2026-08-05/03-hyperliquid-430.png`.

final result: passed for the current rate-limited runtime state; execution-worker readiness is no longer visually conflated with live account truth

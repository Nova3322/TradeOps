# Responsive and browser accessibility acceptance

Date: 2026-08-10

Runtime: `http://127.0.0.1:8022` on a rebuilt Docker Compose image with PostgreSQL ready

Versions: application `143`, stylesheet `62`, service worker `116`

## Result

`PASSED_WITH_EXPECTED_EXTERNAL_WARNING`

Per the product owner's acceptance instruction, this is a browser-only pass; VoiceOver is not part of this acceptance.

- 24 authenticated routes x 4 target viewports = 96 actual-page checks against the current container image.
- Target viewports: 1440 x 900, 1024 x 768, 430 x 932, and 390 x 844.
- No document/main overflow, non-table element clipping, unnamed focusable, unexplained visible disabled control, empty main region, missing primary heading, or incorrect desktop/mobile navigation mode.
- Chromium accessibility trees for all 96 route/viewport combinations contain exactly one banner, one main landmark, one named level-one heading, and no unnamed interactive node.
- At 430 px and 390 px, 28 sampled Tab steps were visible and matched `:focus-visible`; a completed tab cycle returning to `BODY` is retained in the raw report and is not treated as an interactive stop. Keyboard menu open, focus transfer, Escape close, off-canvas visibility, and focus restoration all passed.
- Dark and light choices persisted through reload with matching `aria-pressed`; reduced-motion browser emulation matched and capped sampled motion at `0.01ms`.
- Eight current dark/light Shadow screenshots were visually inspected. Information hierarchy, status distinctions, controls, cards, and long-page flow remain equivalent across themes; 1440, 1024, 430, and 390 px show no visible clipping or horizontal overflow.
- The latest supplied TradingOPS PNG is the loaded header mark and favicon at its original 188 x 194 dimensions and exact SHA-256 `24b27b23e1007ade0de4bdc0bb6880ba087b3116be2b970f89abf84d023432ae`. Its 36 x 36 rendered box, decorative-image semantics, named home link, Apple touch icon, and maskable PWA manifest icon all passed in Chromium; the prior supplied mark is no longer referenced or shipped.
- Desktop dark and 390 px light header captures plus a 512 px rendered PWA icon were visually inspected. The mark remains legible, uncropped, and aligned in both themes and responsive header modes.

## Routes

Primary and configuration routes:

`/`, `/signals`, `/opportunities`, `/opportunities/defaults`, `/proposals/new`, `/reviews`, `/proposals`, `/proposals?history=1`, `/campaigns`, `/campaigns/alerts`, `/orders`, `/risk`, `/shadow`, `/results?environment=SHADOW`, `/notifications`, `/positions`, `/capital`, `/venues`, `/venues/binance`, `/venues/hyperliquid`, `/admin/users`, and `/admin/agents`.

Representative data-backed detail routes:

- one persisted historical proposal;
- one persisted trading task.

The fixture identifiers remain in `report.json` so the evidence can be reproduced against this persisted local data set.

## Defects closed

1. The earlier expanded sweep found the historical-proposal filter row overflowing at 1024 x 768. The content-constrained breakpoint now moves the fourth filter and result count to safe rows; `proposal-history-1024x768-dark.jpg` remains the regression image.
2. The final full unit/API pass found the error state referencing an undefined `--shadow-soft` token. It now reuses the defined `--shadow-quiet` design token; the current browser container serves that correction and the design-token test passes.
3. The previous lettermark, generic SVG app icon, and superseded supplied mark were replaced by the latest TradingOPS mark. The current PNG is preserved byte-for-byte; a square SVG wrapper supplies maskable square framing without redrawing the source artwork.

## Expected external warning

Perptape is not configured in this local runtime. `/opportunities` and the aggregate `/positions` status request `/api/opportunities` once per viewport and receive the expected `503 Service Unavailable`. Each of the eight network responses also produces a paired Chromium console entry, so `report.json` retains 16 browser observations for eight expected external calls.

The pages render the source as unavailable/limited and do not represent it as live, stale-as-current, or zero. `summary.unexpectedBrowserIssues` is empty.

## Evidence files

- `report.json`: raw geometry, labels, disabled-control reasons, browser responses/logs, 96 accessibility-tree summaries, keyboard sequences, theme persistence, reduced-motion results, and derived status.
- `shadow-1440x900-dark.jpg` / `shadow-1440x900-light.jpg`.
- `shadow-1024x768-dark.jpg` / `shadow-1024x768-light.jpg`.
- `shadow-430x932-dark.jpg` / `shadow-430x932-light.jpg`.
- `shadow-390x844-dark.jpg` / `shadow-390x844-light.jpg`.
- `header-1440x900-dark.jpg` / `header-390x844-light.jpg`: responsive brand/header proof.
- `tradingops-icon-512.png`: browser-rendered maskable PWA icon proof.
- `proposal-history-1024x768-dark.jpg`: earlier post-fix regression proof retained with this evidence set.

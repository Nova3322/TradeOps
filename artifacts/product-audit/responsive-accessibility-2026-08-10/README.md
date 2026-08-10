# Responsive and accessibility acceptance

Date: 2026-08-10

Runtime: `http://127.0.0.1:8022` on rebuilt Docker Compose with PostgreSQL ready

Versions: application `142`, stylesheet `60`, service worker `114`

## Result

`PASSED_WITH_EXPECTED_EXTERNAL_WARNING`

- 24 authenticated routes x 4 target viewports = 96 actual page checks.
- Target viewports: 1440 x 900, 1024 x 768, 430 x 932, and 390 x 844.
- No remaining document/main overflow, non-table element clipping, unnamed focusable, unexplained visible disabled button, empty main region, missing primary heading, or incorrect desktop/mobile navigation mode.
- Chrome accessibility trees for all 24 routes contain exactly one banner, one main landmark, one named level-one heading, and no unnamed interactive node.
- At 430 px and 390 px, 28 sampled Tab stops were visible and matched `:focus-visible`; keyboard menu open, focus transfer, Escape close, and focus restoration all passed.
- Eight dark/light Shadow screenshots and one targeted historical-proposal screenshot were visually inspected.

## Routes

Primary and configuration routes:

`/`, `/signals`, `/opportunities`, `/opportunities/defaults`, `/proposals/new`, `/reviews`, `/proposals`, `/proposals?history=1`, `/campaigns`, `/campaigns/alerts`, `/orders`, `/risk`, `/shadow`, `/results?environment=SHADOW`, `/notifications`, `/positions`, `/capital`, `/venues`, `/venues/binance`, `/venues/hyperliquid`, `/admin/users`, and `/admin/agents`.

Representative data-backed detail routes:

- one persisted historical proposal;
- one persisted trading task.

The exact fixture identifiers remain in `report.json` so the evidence can be reproduced against this persisted local data set.

## Defect found and closed

The first expanded sweep found one real defect at 1024 x 768: the historical-proposal filter row kept all four filters and the result count on one row, pushing the count beyond the document edge. The content-constrained breakpoint now moves the fourth filter and count to safe rows. The post-fix 96-check sweep has zero structural failures.

Regression screenshot: `proposal-history-1024x768-dark.jpg`.

## Expected external warning

The raw browser log contains eight `503 Service Unavailable` entries for `/api/opportunities`: one opportunity-page request and one aggregate system-status request at each viewport. Perptape is not configured in this local runtime, so this is the expected fail-closed source state. The UI rendered “external opportunity unavailable” / limited-system truth and did not represent the feed as live, stale-as-current, or zero.

These entries remain intact in the raw report and are classified under `summary.expectedExternalWarnings`; `summary.unexpectedBrowserIssues` is empty.

## Evidence files

- `report.json`: raw per-route geometry, focusable controls, disabled-action reasons, browser logs, keyboard sequence, accessibility-tree results, and derived summary.
- `shadow-1440x900-dark.jpg` / `shadow-1440x900-light.jpg`.
- `shadow-1024x768-dark.jpg` / `shadow-1024x768-light.jpg`.
- `shadow-430x932-dark.jpg` / `shadow-430x932-light.jpg`.
- `shadow-390x844-dark.jpg` / `shadow-390x844-light.jpg`.
- `proposal-history-1024x768-dark.jpg`: post-fix regression proof.

Automated accessibility evidence does not stand in for a human assessment of VoiceOver announcement cadence. That manual check remains in the final stage 9.3 browser pass.

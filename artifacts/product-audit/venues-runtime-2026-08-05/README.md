# Exchange runtime, risk mobile, and reviewer Today audit

Date: 2026-08-05

## Finding

The old QA note that the control plane was not connected to Freqtrade workers is no longer current. Both venue-scoped workers pass bounded identity, futures-mode, dry-run, and exact-catalog probes. The current runtime reports 680 Binance contracts and 265 Hyperliquid contracts, including 88 HIP-3 contracts.

A separate P1 remained in the reviewer workflow: a pure reviewer’s Today page read only pending proposals. A risk-restoration request could require that reviewer’s independent review or execution but would be absent from the console’s only action overview.

## Fix

- Pure-reviewer Today now reads the same risk-control fact source as `/risk`.
- Proposal reviews and risk-restoration work are shown as separate, non-overlapping responsibilities.
- A reviewable or executable restore request contributes one risk-restoration task and links to `/risk`.
- Failure to read risk-control status displays `—`, an explicit read failure, and a needs-attention state rather than a false zero or green all-clear headline.
- The reviewer role still has no proposal creation, capital, trade-operation, or access-management entry.

## Five-dimensional acceptance

1. Code: one testable reviewer-workload projection combines proposal and risk-restoration duties.
2. API: a pure reviewer can read risk conditions and role-specific actions; worker status comes from two live, bounded dry-run probes.
3. Actual page: the current pure-reviewer Today page shows 14 independent proposal reviews and 0 risk-restoration tasks. Its visible mobile menu contains only Today, Review queue, and System status. The administrator risk page was inspected at 390 px, and system status shows the current Freqtrade contract scope.
4. End to end: the browser was switched from administrator to pure reviewer and back. No restore request, review, policy, authorization, order, or capital state was mutated.
5. Tests: 19 API/web tests and 4 isolated PostgreSQL integration tests passed, covering the six required identity classes and both reviewed and direct-administrator restoration paths.

## Current external/runtime limits

- Binance read-only account facts: connected.
- Hyperliquid read-only account facts: upstream rate-limited; old snapshots remain historical only.
- Perptape: connected.
- NoTilt: production scope configuration incomplete.
- Telegram long polling: healthy, but this audit did not claim real Telegram Web/mobile client interaction.
- All order, capital, automatic-scaling, signing and broadcast capabilities remain disabled.

## Evidence

- `01-binance-account-desktop.png`: current Binance account facts and read-only boundary.
- `02-risk-390.png`: 390 px administrator risk page with explicit per-condition blockers.
- `03-reviewer-today-desktop.png`: pure-reviewer Today after the fix.
- `04-reviewer-today-390.png`: 390 px pure-reviewer Today.
- `05-reviewer-today-430.png`: 430 px pure-reviewer Today.
- `06-system-status-desktop.png`: live Freqtrade worker and HIP-3 scope projection.

Final result: passed for the current runtime state; reviewer Today no longer omits risk-restoration duties.

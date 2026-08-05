# Today → review → proposal launch-window audit

Date: 2026-08-05

## Finding

The Today page counted two approved proposals as launchable work even though both proposal launch windows had already expired. The same records appeared in Current proposals, and their detail pages still exposed risk-check and authorization guidance. The server would reject subsequent execution, so the page was presenting actions that could not succeed.

History statistics also overlapped approved launch-window-expired records with the expired count. That made the category totals larger than the history total.

## Fix

- Added a server-owned `execution_status` projection: `AWAITING_LAUNCH`, `WINDOW_EXPIRED`, or `TRADE_CREATED`.
- Kept the immutable decision status (`APPROVED`) unchanged for audit truth.
- Today counts only approved proposals that are still within their launch window and have no trade task.
- Current proposals excludes expired approved records; History labels them as `启动窗口已过期`.
- Expired approved detail pages are read-only and explain that a new proposal is required.
- History summary categories are mutually exclusive for operations: entered trading, expired, and rejected.
- Early terminal proposals show their actual terminal time rather than a future expiry countdown.

## Five-dimensional acceptance

1. Code: shared execution-state projection and UI helpers drive Today, lists and detail.
2. API: list/detail preserve `status=APPROVED` while returning the correct execution status.
3. Actual page: Today, Current proposals, History and an expired-approved detail were inspected from the running local service.
4. End to end: the same proposal moved out of current work, remained in audit history, and exposed no risk, authorization or trade-creation action.
5. Tests: API/web contract and isolated PostgreSQL workflow tests cover pre-expiry and post-expiry projections.

No proposal review, authorization, order, capital action or Gate mutation was performed.

## Evidence

- `01-today-admin-desktop.png`: before, false approved-awaiting-launch work.
- `02-review-queue-admin-desktop.png`: real independent review queue.
- `03-today-admin-after.png`: Today after removing false launch work.
- `04-current-proposals-after.png`: only current proposals remain.
- `05-history-approved-expired.png`: approved decision retained with launch-window-expired outcome.
- `06-approved-expired-detail-desktop.png`: read-only desktop detail.
- `07-approved-expired-detail-390.png`: 390 px detail, no horizontal overflow.
- `08-approved-expired-detail-430.png`: 430 px detail, no horizontal overflow.

Final result: passed for the current local dataset; expired approvals no longer masquerade as actionable work.

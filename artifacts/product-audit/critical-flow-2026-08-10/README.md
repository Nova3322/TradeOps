# Critical-flow and offline-recovery acceptance

Date: 2026-08-10

Runtime: `http://127.0.0.1:8022` on rebuilt Docker Compose with PostgreSQL ready

Versions: application `143`, stylesheet `61`, service worker `115`

## Result

`PASSED_WITH_EXTERNAL_AND_MANUAL_FOLLOW_UPS`

- A newly created team remained a separate isolation boundary and redirected a direct opportunity-route visit to the fail-closed setup page.
- The production team exposed the exact Perptape unavailable state while keeping manual proposal creation available; no stale or sample opportunity was substituted.
- Capital showed incomplete and stale sources separately, blocked the current aggregate net-worth claim, preserved labelled history, and kept funding disabled.
- An expired frozen proposal remained terminal: review was expired, risk was not run, authorization was not issued, and no progression action was exposed.
- A real API outage originally produced only a generic read failure. The page now states reason, impact, responsible role, next step, and technical code; focus moves to the alert heading. Retrying after service recovery restored the requested route without an alert.
- The same outage flow was accepted in Chinese and English. Missing English shell labels discovered during the pass were completed for reports, Shadow, notifications, scope selection, and offline guidance.
- No proposal approval, risk authorization, order, funding, signing, transfer, or broadcast action was submitted during acceptance.

## Five-dimensional evidence

- Code: centralized error guidance covers network interruption, timeout, forbidden scope, missing risk policy, and unknown failures. The error card is keyboard-focused, announced as an assertive alert, responsive, and bilingual.
- Database: no entity or migration change. The pass used existing Workspace, team, account, proposal, risk, capital, and audit truth instead of creating a second state source.
- API: the real pages were backed by the PostgreSQL-ready Compose API. The opportunity 503 remained the expected external Perptape-unconfigured response; the API outage and recovery were induced and observed rather than mocked.
- Actual pages: team setup, opportunities, capital, an expired proposal, and Chinese/English offline states were opened and visually inspected in the in-app browser. Retry recovery returned to the requested trade page and removed the alert.
- End-to-end runtime: Compose was rebuilt with application `143`, stylesheet `61`, and service worker `115`; `/health/ready` returned `ready` with `durable_store=postgresql` after recovery.
- Tests: 70 isolated PostgreSQL integration tests passed across core workflow, Shadow, team scope, access, accounts, signals, notifications, risk restore, Agent API, and results/audit runtime. All 23 health/web-shell tests passed, including error semantics, focus, translation, and cache-version checks.

## Screenshots

- `01-team-setup-fail-closed.png`: new-team setup boundary and safe enablement path.
- `02-opportunities-unavailable.png`: Perptape unavailable without false live or zero substitution.
- `03-capital-partial-stale.png`: partial/stale capital facts and blocked aggregate.
- `04-expired-proposal-terminal.png`: expired frozen proposal with no review/risk/authorization progression.
- `05-offline-guidance-fixed.png`: Chinese logged-in offline guidance with focused heading and retry.
- `06-offline-guidance-en.png`: English logged-in offline guidance with the same hierarchy and actions.

## Remaining follow-ups

- External: configure a real Perptape key before claiming live opportunity readiness.
- Manual accessibility: VoiceOver announcement cadence is still open. Automated focus and accessibility-tree evidence do not replace the human listening pass.
- Legal/product: the open-source license choice remains a separate stage 8.3 decision.

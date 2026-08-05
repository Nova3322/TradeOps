# Proposal review decision flow acceptance

Date: 2026-08-05

Scope: review queue, frozen SYSTEM proposal detail, approval second confirmation, responsive layout, and six-identity permission projection.

## Result

- Queue decisions lead with notional value, maximum loss and remaining time.
- Stored legacy Chinese rationale is presented as a creation-time snapshot without changing the frozen record.
- New automatic proposal rationale also uses creation-time semantics.
- Desktop, 390 px and 430 px views do not overflow horizontally.
- Approval confirmation was opened and cancelled; no review, authorization, order or send occurred.
- Administrator, proposer, reviewer, treasury, observer and disabled identities were exercised through live read-only API checks; all except the disabled identity were also logged into the actual console during this run.

## Evidence

1. `01-proposal-detail-desktop.png`
2. `02-proposal-detail-390.png`
3. `03-proposal-detail-430.png`
4. `04-approval-confirm-430.png`
5. `05-reviewer-queue-desktop.png`

## Remaining limits

- The run deliberately did not submit a real approval or rejection. The mutation path was verified against the isolated PostgreSQL test database.
- External Perptape/venue availability is not inferred from a frozen proposal snapshot; the proposal only records what was ready when it was created.

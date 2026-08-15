# TradingOPS API Key quickstart

The running [`/openapi.json`](/openapi.json) document is the complete API
contract. This guide covers authentication and safety semantics.

## Identity and authorization

An API Key is a user credential. It is bound to one Workspace and Team context,
but it has no independent Account or Venue scope and stores no role copy. Every
request uses the owning user's current RBAC permissions; resource APIs check the
exact Team, Account, and Venue at request time.

Create a key from **user menu → API Key** by opening the collapsed creation
panel. Creation always uses the current Workspace and Team; switch context
first to create a key elsewhere. "My API Keys" lists only keys created by the
current account. Plaintext is displayed once after creation or rotation. Store
it in a secret manager, never in source, prompts, screenshots, chat, or logs.

```bash
export BASE_URL="BASE_URL"
export API_KEY="API_KEY"
export WORKSPACE_ID="WORKSPACE_ID"
export TEAM_ID="TEAM_ID"

curl --fail-with-body \
  --header 'Authorization: Bearer API_KEY' \
  --header 'Accept: application/json' \
  'BASE_URL/api/api-key/connection'
```

Require HTTP 200, `connected=true`, matching `WORKSPACE_ID` and `TEAM_ID`, and
`scope.scope_model=USER_RBAC`. Do not send a login Cookie with a Bearer key.
The legacy `/api/api-client/connection` route and legacy response aliases remain
available for compatibility.

## Read and write boundaries

Start with read-only endpoints such as `/api/instruments`, `/api/opportunities`,
`/api/proposals`, `/api/campaigns`, `/api/results`, and `/api/audit`. Preserve
environment, source, and freshness fields. Missing, stale, lost, incomplete, or
rate-limited data is neither real-time data nor numeric zero.

The current execution environments are `TESTNET` and `LIVE`; `SETUP` is an
internal Team configuration state and is not valid proposal or order scope.
The server derives new proposal, authorization, intent, and execution scope from
the Team's persisted current mode. A client-supplied conflicting environment is
rejected rather than used as a routing instruction.

`LIVE` identifies a production environment but does not enable execution.
Writes require the current user permission, exact resource authorization,
idempotency, independent review, risk checks, and every server-side gate.
`LIVE_ORDER_SEND`, `CAPITAL_TRANSFER`, `SIGNING`, and `BROADCAST` remain disabled
unless separately enabled through governed controls.

API Key lifecycle operations require an interactive user session. A key may be
disabled, rotated, or permanently revoked; owner deactivation or RBAC removal
takes effect on the next request.

See the [Chinese API Key guide](API_KEY_QUICKSTART.md) for the equivalent
workflow.

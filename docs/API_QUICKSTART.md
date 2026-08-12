# TradingOPS API quickstart

The running [`/openapi.json`](/openapi.json) document is the only complete API
contract. This guide covers authentication, scope, read-only access, and safety;
it does not duplicate every field.

## 1. What the API does

TradingOPS exposes governed views of instruments, opportunities, frozen
proposals, approvals, operations, results, notifications, and audit events. An
API Client inherits the current human owner's roles dynamically and is fixed to
one Workspace, Team, Account, and Venue.

It does not return passwords, login cookies, exchange secrets, wallet keys,
signing material, or broadcast credentials.

## 2. Configure placeholders

```bash
export BASE_URL="BASE_URL"
export TOKEN="TOKEN"
export WORKSPACE_ID="WORKSPACE_ID"
export TEAM_ID="TEAM_ID"
export ACCOUNT_ID="ACCOUNT_ID"
```

Create a Token from **top-right user menu → Personal Center → API Access**.
Plaintext is shown once after creation or rotation. Store it in a secret manager,
not source, prompts, screenshots, chat, or logs.

## 3. Validate identity and scope

```bash
curl --fail-with-body \
  --header 'Authorization: Bearer TOKEN' \
  --header 'Accept: application/json' \
  'BASE_URL/api/api-client/connection'
```

Require HTTP 200, `connected=true`, and an exact match for `WORKSPACE_ID`,
`TEAM_ID`, and `ACCOUNT_ID`. Do not send a login Cookie with a Bearer Token;
mixed credentials are rejected.

## 4. Common read-only requests

```bash
curl -H 'Authorization: Bearer TOKEN' 'BASE_URL/api/instruments'
curl -H 'Authorization: Bearer TOKEN' 'BASE_URL/api/opportunities'
curl -H 'Authorization: Bearer TOKEN' 'BASE_URL/api/proposals'
curl -H 'Authorization: Bearer TOKEN' 'BASE_URL/api/campaigns'
curl -H 'Authorization: Bearer TOKEN' 'BASE_URL/api/results?environment=SHADOW'
curl -H 'Authorization: Bearer TOKEN' 'BASE_URL/api/audit?environment=SHADOW&limit=200'
```

Use `/openapi.json` to check current parameters and permissions. There is no
universal cursor/offset/page protocol. Notifications accept `limit=1..200`;
audit accepts `limit=1..500`.

## 5. Python

```python
import json
from urllib.request import Request, urlopen

BASE_URL = "BASE_URL"
TOKEN = "TOKEN"
WORKSPACE_ID = "WORKSPACE_ID"
TEAM_ID = "TEAM_ID"
ACCOUNT_ID = "ACCOUNT_ID"

request = Request(
    f"{BASE_URL.rstrip('/')}/api/api-client/connection",
    headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"},
)
with urlopen(request, timeout=10) as response:
    connection = json.load(response)

assert connection["connected"] is True
assert connection["scope"]["workspace_id"] == WORKSPACE_ID
assert connection["scope"]["team_id"] == TEAM_ID
assert connection["scope"]["account_id"] == ACCOUNT_ID
```

## 6. Data and environment semantics

- `SHADOW` is simulated and must never be described as a real fill, position, or
  balance.
- `LIVE` identifies a production environment; it does not prove that order send,
  capital transfer, signing, or broadcast is enabled.
- Preserve provider/source and `as_of`, `observed_at`, `fetched_at`, and
  `data_status` fields.
- Missing, stale, lost, incomplete, expired, or rate-limited data is not
  real-time data and is not numeric zero.

## 7. Errors, writes, and idempotency

Typical errors include `AGENT_TOKEN_INVALID`, `AGENT_TOKEN_EXPIRED`,
`API_CLIENT_SCOPE_DENIED`, `RBAC_DENIED`, `HUMAN_WEB_CONFIRMATION_REQUIRED`,
`IDEMPOTENCY_CONFLICT`, `VERSION_CONFLICT`, and `API_CLIENT_RATE_LIMITED`.
Retry only when the response says the operation is retryable, and use bounded
backoff.

AI clients are read-only by default. A write requires explicit authorization,
an OpenAPI-defined endpoint, sufficient current role and exact scope, a unique
`idempotency_key` in the request body, and all server-side review, risk,
freshness, and capability gates. Unknown outcomes must be queried and
reconciled before retry.

`AUTO_ADD`, `AUTO_OPERATING_REFILL`, `AUTO_PROFIT_SWEEP`, `CAPITAL_TRANSFER`,
and `LIVE_ORDER_SEND` remain disabled until independently configured and
authorized. The server is the final authority.

For a copy-ready AI system prompt, see
[`AI_API_QUICKSTART.md`](AI_API_QUICKSTART.md).

# Security Policy

## Supported code

Security fixes target the current default branch and the latest published release. Historical
commits, local research prototypes, generated evidence, and user-created deployment forks are not
maintained security releases.

## Reporting a vulnerability

Use the repository host's private vulnerability-reporting channel or a private security advisory.
Do not open a public issue containing credentials, account identifiers, orders, positions, wallet
addresses tied to a person, or a working exploit against a deployed instance.

Private reporting contact: `gaargrg@gmail.com`. Repository advisory channel:
<https://github.com/Nova3322/TradeOps/security/advisories/new>.

Include the affected commit/release, component, impact, reproducible preconditions, and the smallest
redacted proof needed to validate the issue. Never send production API keys, private keys, database
dumps, session cookies, Agent tokens, or notification credentials.

## Operational response

Credential exposure is handled as compromise: revoke and rotate first, keep order/capital/signing
switches and database gates disabled, preserve audit evidence, reconcile external facts, and only
then restore read-only service. Recovery never re-enables risk or funds automatically.

The control plane is fail closed. A successful account connection check, passing test, or available
UI does not certify production trading. LIVE order send, capital transfer, automatic add, automatic
profit sweep, and automatic operating refill remain separately gated.

# TradingOPS Governance

## Principles

TradingOPS is governed as a safety-critical open-source control plane. Decisions
prioritize fail-closed behavior, least privilege, independent review, auditable
state, scope isolation, and reproducible evidence over feature speed.

## Roles

- **Contributors** propose changes and provide evidence under the CLA.
- **Maintainers** review code, documentation, tests, licenses, privacy, and
  security implications.
- **Release managers** verify the release checklist and may block a release.
- **Security responders** receive private reports and coordinate remediation.

One person may hold multiple roles, but a maintainer may not use repository
roles to bypass TradingOPS runtime separation-of-duty controls.

## Decision process

1. Routine changes use pull-request review and passing required checks.
2. Changes to permissions, review rules, risk, idempotency, audit, environment
   separation, external side effects, licensing, or governance require an ADR or
   equivalent design note and approval from two maintainers where available.
3. Security embargoes may use a private process, followed by a public advisory
   and changelog entry when disclosure is safe.
4. Release managers may block releases for unresolved P1 findings, secret or
   license uncertainty, missing migration evidence, or unsafe defaults.

## Project assets

Source, releases, package names, signing keys, domains, and commercial license
rights must be controlled by the documented project owner or organization.
Maintainers must not place personal credentials or private infrastructure in the
repository.

## Changes to governance

Governance changes use a public pull request, a stated rationale, and a minimum
seven-day review window unless an urgent security issue requires temporary
measures. Temporary measures must be revisited after the incident.

# Changelog

All notable changes are documented here using Keep a Changelog conventions and
Semantic Versioning where compatible with migration and API contracts.

## [Unreleased]

### Changed

- Replaced the retired SHADOW simulator with strict TESTNET/LIVE runtime scope;
  `SETUP` remains an internal unconfigured Team state only.
- Moved the only Team mode switch to Mode Settings and kept Account Management
  as a TESTNET/LIVE configuration filter with environment-scoped credential,
  lifecycle, and deletion checks.
- Kept Perptape opportunities separate from signed Webhook signals, linked
  opportunity details to the upstream market scanner using its raw symbol-only
  query, and aligned directional presentation across responsive themes.
- Made API Key creation a visible, collapsed-on-load action bound to the current
  Workspace and Team, limited inventory to the current owner's keys, and kept
  OpenAPI as the complete field contract.
- Renamed the member-access OPERATOR preset to Risk Management and clarified its
  reviewed risk-policy, pause, authorization, reduction, and reconciliation duties.
- Assigned Vault/Safe configuration to the Capital Center while Performance
  Reports owns multi-account equity history, trusted aggregation, range control,
  and fullscreen chart presentation.
- Allowed NoTilt Vault and Safe Spending Limits to remain configured together
  while selecting exactly one provider for each newly frozen capital operation.
- Prepared the repository for public release with a GPL-3.0-only plus commercial
  dual-license model.
- Added public onboarding, API/AI integration, governance, support, security,
  contribution, SBOM, third-party notice, and release documentation.
- Replaced local identity literals with configurable synthetic defaults.
- Restricted public artifacts to sanitized fixture screenshots.
- Upgraded `cryptography` to 50.0.0 after dependency audit identified fixed
  2026 advisories in the prior lockfile.

### Security

- Added current-tree and history secret-scanning workflows and a non-destructive
  history remediation plan.
- Kept order, capital transfer, signing, broadcast, and automation capabilities
  disabled by default.

## [0.1.0] - 2026-08-12

### Added

- Initial fail-closed TradingOPS control-plane implementation with Workspace and
  Team isolation, frozen proposals, independent review, risk controls,
  idempotent execution, audit, reconciliation, and human-owned API clients.

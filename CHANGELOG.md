# Changelog

All notable changes are documented here using Keep a Changelog conventions and
Semantic Versioning where compatible with migration and API contracts.

## [Unreleased]

### Changed

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

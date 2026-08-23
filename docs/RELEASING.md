# Release process

No release is created from an unreviewed working tree.

The approved public identity is `Nova3322`, with licensing and private security
contact `165258092+Nova3322@users.noreply.github.com`. Local `refs/codex/snapshots/*` are tool state and
must never be pushed.

1. Confirm the intended commit and clean public boundary.
2. Run `python scripts/generate_publication_metadata.py --check`.
3. Run Gitleaks on the public tree and complete Git history.
4. Review `THIRD_PARTY_NOTICES.md`, `sbom.cdx.json`, dependency vulnerabilities,
   and license compatibility.
5. Run formatting, lint, typing, migrations, unit, integration, API, and browser
   end-to-end tests against disposable infrastructure.
6. Start from a copy of `.env.example`; verify `/health/live`, `/health/ready`,
   `/openapi.json`, login, roles, API Key lifecycle, and all persistent gates.
7. Inspect 1440, 1024, 430, and 390 screenshots for both themes. Screenshots must
   use synthetic fixtures and contain no credential or private operational data.
8. Update `CHANGELOG.md`, version metadata, and release notes.
9. Require release-manager approval. Tag with a signed tag and publish checksums,
   SBOM, provenance, and source after all P1 findings are closed.

A failed, incomplete, stale, or rate-limited check is not a pass. Historical
secret findings require rotation and an approved history-remediation procedure
before public publication.

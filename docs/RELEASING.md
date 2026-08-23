# Release process

No release is created from an unreviewed working tree. The public repository
builds release images; the private `Nova3322/tradeops-ops` repository owns every
production coordinate and promotion action.

The approved public identity is `Nova3322`, with licensing and private security
contact `165258092+Nova3322@users.noreply.github.com`. Local
`refs/codex/snapshots/*` are tool state and must never be pushed.

## Public source and image publication

1. Confirm the intended full commit SHA and clean public boundary.
2. Run `python scripts/generate_publication_metadata.py --check`, Gitleaks on the
   public tree and complete retained history, and the repository CI matrix.
3. Review `THIRD_PARTY_NOTICES.md`, `sbom.cdx.json`, dependency vulnerabilities,
   license compatibility, version metadata, Schema Revision, and release notes.
4. Merge through a reviewed pull request. `.github/workflows/publish-image.yml`
   runs only after the `CI` workflow succeeds for a push to public `main` and
   checks out that exact tested SHA.
5. Build and push one OCI image index for `linux/amd64` and `linux/arm64`. The
   workflow publishes the immutable source tag for discovery, but release and
   deployment coordinates always use
   `ghcr.io/nova3322/tradeops@sha256:<manifest-digest>`; `latest` is not used.
6. Record and verify the image index contains both required platforms. Inspect
   the selected platform image labels for the full source SHA, project version,
   and Schema Revision. Retain the BuildKit SBOM and GitHub build-provenance
   attestations associated with the same manifest digest.

The public workflow contains no production host, domain, SSH credential,
runtime secret, production Compose file, or deployment command.

## Private manual promotion

1. Open a pull request in the private Ops repository that pins the exact public
   source SHA, multi-platform manifest digest, version, and Schema Revision in
   `release.yaml`.
2. Keep the contract `pending` during preparation and preflight. A pending
   contract is deliberately non-deployable.
3. On the production `aarch64` host, authenticate to GHCR with a read-only
   package credential, pull the image by manifest digest, and verify Docker
   selects `linux/arm64`. Recheck the image labels before promotion approval.
4. Review the public CI run, SBOM, provenance, database compatibility, validated
   backup, application rollback, and manual database-restore procedure.
5. Only a separate, approved production-promotion pull request may change the
   release to `active`. Merging that private change enters the protected
   `production` Environment and its manual approval gate.

A failed, incomplete, stale, cancelled, or rate-limited check is not a pass.
Production image publication does not enable trading, capital movement, or any
runtime capability gate.

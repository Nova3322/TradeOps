# Public repository boundary

The public source tree contains product code, migrations, synthetic tests,
vendored fonts and notices, public documentation, a curated screenshot gallery,
and reproducible CI/release metadata.

It excludes all local or operator-owned state:

- `.env`, `.env.*` other than `.env.example`, secret files, credentials, and cookies;
- `.local/`, databases, dumps, caches, logs, raw test evidence, and incident records;
- private strategies, imported research, personal workbooks, and local notes;
- real Workspace, Team, user, account, wallet, order, position, balance, or provider data; and
- screenshots that have not been reviewed against the synthetic-fixture checklist.

`scripts/verify_public_release.py` builds the prospective public tree from
tracked and non-ignored files only, validates the license and safe configuration
contract, rejects personal absolute paths and populated secret fields, and can
scan the snapshot with Gitleaks. Ignored local state is deliberately not copied
into that snapshot.

Git history is a separate publication surface. A clean prospective tree does
not make historical credentials safe. The existing history must pass the
redacted full-history scan and the rotation/remediation procedure in
[`security/HISTORY_CLEANUP_PLAN.md`](security/HISTORY_CLEANUP_PLAN.md) before an
existing-history publication is approved.

The release manager must inspect the exact candidate rather than relying on a
developer checkout. See [`RELEASING.md`](RELEASING.md).

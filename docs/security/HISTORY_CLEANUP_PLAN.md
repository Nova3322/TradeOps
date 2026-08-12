# Local-only credential cleanup plan

This plan covers a credential-bearing commit that exists only behind a local
tool snapshot reference. The commit is not reachable from any branch, tag, or
remote-tracking reference intended for publication.

## Safety boundaries

- Revoke or rotate every affected credential before changing local Git objects.
- Keep the existing repository and remote URL unchanged and private.
- Do not rewrite normal branches or tags.
- Do not push local tool snapshot references.
- Do not force-push, make the repository public, or publish a release as part of
  this cleanup.
- Keep ignored local configuration and private operating data outside the public
  candidate without printing their values in reports or logs.

## Cleanup workflow

1. Verify that each affected credential has been rotated and that its previous
   value or signing authority is inactive.
2. Record the current HEAD, worktree state, remote configuration, branches,
   tags, remote-tracking references, and the exact local snapshot references
   containing the affected commit.
3. Create a permission-restricted private backup containing the complete Git
   object database, references, worktree, ignored private files, and uncommitted
   changes. Generate a SHA-256 manifest and verify a restore in an isolated
   directory.
4. Confirm immediately before deletion that the affected commit is reachable
   only from the expected local snapshot references.
5. Delete only those snapshot references, using their expected object IDs as a
   compare-and-swap guard. Preserve all branches, tags, remote-tracking
   references, and the configured remote.
6. Remove objects made unreachable exclusively by those deleted snapshot
   references. Preserve unrelated pre-existing dangling objects during this
   step, and verify that every published reference retains the same tip and
   commit graph.
7. Scan the complete public candidate tree and every branch, tag, and other Git
   reference intended for publication. Both scopes must report zero leaks.
8. Run the full test and release-check matrix, then commit only the intended
   public-release files locally.

## Required evidence

- Private backup path, permissions, SHA-256 manifest, and restore result.
- Snapshot references deleted and their redacted containment result.
- Before/after HEAD and hashes for all normal branch, tag, and remote-tracking
  tips, demonstrating that they did not change during object cleanup.
- Secret-scan tool versions, scan scopes, exclusion policy, and zero-finding
  reports for the public tree and every proposed public reference.
- Unit, integration, API, migration, end-to-end, and release-check results.
- Final local commit contents and a publication checklist that still requires
  explicit approval before any remote or visibility change.

## Release decision

- **Block publication** while any affected credential remains active.
- **Block publication** if the affected commit is reachable from a proposed
  public reference or if either required scan reports a finding.
- **Block publication** if normal branch, tag, or remote-tracking tips change
  during the local snapshot cleanup.

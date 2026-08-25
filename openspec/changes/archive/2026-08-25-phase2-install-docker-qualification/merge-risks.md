# Governed Merge Risks

## Integration risks

| Risk | Handling |
|---|---|
| Linux filesystem lacks `O_TMPFILE` or safe unnamed publication | use only the receipt-bound named-temp fallback; otherwise fail closed |
| Manager repository contains non-canonical local Git config | reject adoption and require explicit reconciliation; never execute its hooks as root |
| Provider CLI cannot emit structured live login/quota status | fail qualification before smoke; do not infer availability or retry quota use |
| Toolchain archive contains group/other-writable nested members | reject the archive; candidate preparation must emit canonical non-writable modes |
| Existing account private GID is shared by another passwd/group member | reject adoption; do not silently remove or reassign external identities |
| Rollback cannot stop an active service | enter `rollback-blocked` and retain credentials/assets rather than tearing runtime out from under the service |

## Known release blockers

- A fresh **protected exact-SHA** qualification is still required. The latest local
  dummy-credential run passed installer, R9, attestation, and service gates, then
  stopped at the expected provider-auth boundary; it is not provider/live-release
  evidence.
- #665 is source-resolved by the strict-compatible `/usr/bin/node --jitless` wrappers
  for `srt` and `openspec`; the protected RC must still prove both wrappers in their
  real systemd units while retaining `MemoryDenyWriteExecute=yes`.
- #692 is source-resolved: job and shim HOME validation now fail closed for missing,
  relative, symlinked, wrong-owner, and unresolved-account paths.
- #763 is source-resolved: Manager receives exactly one GitHub HTTPS credential helper,
  and repair recovery rejects absent or incomplete gate-ledger worktree state.
- PR review/merge, CI check runs, OpenSpec archive, and the exact-SHA RC evidence have
  not yet been produced on the default branch.

These are release blockers, not accepted release residuals. Source closeout may archive
the OpenSpec change once its implementation checklist and repository gates are complete,
but the release workflow must still reject the final default-branch SHA until the protected
exact-SHA RC evidence, review/merge, and version policy are all satisfied. `v0.2.0` must
not be tagged or released while any blocker remains.

## Rollback plan

The work exists only on the local feature branch. If an integrated regression cannot
be repaired, revert the scoped feature commits in reverse integration order and retain
the failed qualification evidence. No production installation, remote branch, tag,
GitHub Release, or PyPI state needs rollback because none is changed by this work.

## Process deviation

During adversarial review, a reviewer extended a local Copilot response-shape probe
into one minimal live smoke despite the no-quota instruction. The command returned the
expected response and reported `premiumRequests=6`. It is not qualification evidence,
was not retried, and all subsequent provider/network probes were stopped. No credential
content was recorded.

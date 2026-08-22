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

- R9 still rejects the current contract because Builder can legally delete within the
  Tier-1 `repo-worktree`; D6 does not allow a waiver.
- #665 still lacks a successful real-systemd W+X qualification.
- #692 still leaves runtime `HOME` fail-open.
- A successful exact-SHA provider, Manager Git, and full dispatch qualification has
  not been produced.

These are release blockers, not accepted release residuals. The OpenSpec change must
remain unarchived and `v0.2.0` must not be tagged or released while any remains.

## Rollback plan

The work exists only on the local feature branch. If an integrated regression cannot
be repaired, revert the six source commits in reverse integration order and retain the
failed qualification evidence. No production installation, remote branch, tag,
GitHub Release, or PyPI state needs rollback because none is changed by this work.

---
status: accepted
work_item: legacy-workspace-proof-helper
issue: TBD
domain_breadth: 0
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# F — workspace proof helper 規格

## Authority and scope

Implement the Builder-side read-only helper that observes one exact historical workspace. Manager cannot traverse the Builder-owned 0700 clone.
Blocked by E contract and consumes #968 A1 identity and #969 marker-writer contracts. G separately owns installed cross-UID execution/transport.

## Requirements

- **R1** — Verifier accepts only an exact workspace reference, request nonce, expected repository/slice/worktree identity, and trusted caller context; it never searches by name, branch, task or suffix.
- **R2** — Real Git/filesystem observation validates physical root, Git top-level/git-dir, canonical origin, HEAD/branch, and both .git directory clones and .git file linked worktrees; canonical origin must match expected repository identity.
- **R3** — Missing pre-#969 marker is reported as absent_pre_identity_schema; malformed or mismatched existing marker fails. No marker is written and no marker proves Work Item ownership.
- **R4** — Immutable typed proof separates expected inputs from observed path/Git facts and binds request, nonce, helper version, observed_at and canonical digest. It does not assert historical Work Item lineage.
- **R5** — Tests cover real isolated clone and linked-worktree cases, wrong repo/path/origin, symlink, malformed .git file, marker states and no-write behavior; G owns real separate-UID acceptance.
- **R6** — Missing/unreadable Git facts keep the target legacy_unbound/needs_human; no same-name alternative is tried.

## Acceptance and verification

- [ ] Verifier accepts only an exact workspace reference, request nonce, expected repository/slice/worktree identity, and trusted caller context; it never searches by name, branch, task or suffix.
- [ ] Real Git/filesystem observation validates physical root, Git top-level/git-dir, canonical origin, HEAD/branch, and both .git directory clones and .git file linked worktrees; canonical origin must match expected repository identity.
- [ ] Missing pre-#969 marker is reported as absent_pre_identity_schema; malformed or mismatched existing marker fails. No marker is written and no marker proves Work Item ownership.
- [ ] Immutable typed proof separates expected inputs from observed path/Git facts and binds request, nonce, helper version, observed_at and canonical digest. It does not assert historical Work Item lineage.
- [ ] Tests cover real isolated clone and linked-worktree cases, wrong repo/path/origin, symlink, malformed .git file, marker states and no-write behavior; G owns real separate-UID acceptance.
- [ ] Missing/unreadable Git facts keep the target legacy_unbound/needs_human; no same-name alternative is tried.

## Non-goals

No Manager traversal, chmod/ACL, registry or checkpoint writes, operator auth, migration action, recovery transition, reclaim or proof transport deployment.

## Verification boundary

此三件套 status=accepted 只代表 planning scope 已寫完整，不代表外部 issue owner acceptance、產品實作、測試、CI、merge 或 #547 closure。

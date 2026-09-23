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

# F — workspace proof helper Tasks

## Tasks

- [ ] **T01**：Freeze typed proof fields and canonical digest with E.
- [ ] **T02**：Implement read-only exact-path verifier and actual Git/root/origin checks.
- [ ] **T03**：Test clone and linked-worktree layouts plus adversarial path/marker cases.
- [ ] **T04**：Verify success/failure do not mutate workspace bytes or config.
- [ ] **T05**：Run focused tests and report that G's real separate-UID runner gate remains outstanding.

## Sizing and completion record

Five dimensions: verified current_sizing_snapshot output = 5 / Yellow (0/1/2/0/2) under fix-standard. Artifact refs are the sibling spec, design and todo. Record implementation/test/review/CI/merge/runtime evidence separately.

## Non-goals

No Manager traversal, chmod/ACL, registry or checkpoint writes, operator auth, migration action, recovery transition, reclaim or proof transport deployment.

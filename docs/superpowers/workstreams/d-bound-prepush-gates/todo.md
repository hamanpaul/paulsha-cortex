---
status: accepted
work_item: d-bound-prepush-gates
domain_breadth: 1
state_consistency: 1
invariant_count: 5
artifact_classes:
  - source
  - tests
  - documentation
---

# Push 前 D-bound local gates（#973 Child C1）

## Boundary

- Scope: manager.py Candidate-bound gate invalidation/rerun and work_bridge.py local exact-D preflight handoff.
- Dependencies: #972 merged and B3 proof/adoption of true D. This is C aggregate's pre-push implementation slice.
- C1 ends at ready-to-push exact D. C2 owns remote push, existing/new PR reconciliation, post-push PR CI and existing-authority closeout.
- No remote delivery side effect, new schema, journal transition or authority is added.
- See the [issue-backed C split](../../../../issue-backed-descendants.md).

## Tasks

- [ ] T1 Prove every C-bound build/verify/test/review/maintainer-review/preflight/CI record is stale after Candidate C→D; #847 shortcut does not apply.
- [ ] T2 Obtain authoritative build harvest, verify/tests, cross-domain review and policy-required maintainer review bound to exact D.
- [ ] T3 Run local exact-head preflight on D and verify target/evidence SHA exactness.
- [ ] T4 Stop before any remote action on missing, failed, stale, wrong-head or ambiguous local evidence.
- [ ] T5 Emit ready-to-push exact-D decision only after every required local gate passes; never query PR CI before push.
- [ ] T6 Verify no push/PR/merge/issue/run closeout occurs in C1; cover C evidence rejection and D pass/failure matrix.

## Sizing

Declared dimensions are domain_breadth=1 (manager.py and work_bridge.py) and state_consistency=1 (existing local gate evidence only, no remote durable transition). Accepted fix-standard artifacts compute to 6 / Yellow.

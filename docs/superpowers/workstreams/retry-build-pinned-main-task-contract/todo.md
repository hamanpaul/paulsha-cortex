---
status: accepted
work_item: retry-build-pinned-main-task-contract
domain_breadth: 0
state_consistency: 1
invariant_count: 5
artifact_classes:
  - source
  - tests
  - documentation
---

# 固定 C/M Builder task helper（#973 B1c）

## Boundary

- Scope: work_actions.py pinned task helper only.
- Dependencies: #972 tuple plus B1a exact-M source-pin identity/ref-name contract. B1b provisioning is a later runtime step, not a pre-reset task-helper dependency.
- Shared by operator retry-build and B2 automatic caller; B0 deterministic reservation id is required only for automatic path.
- B1b imports/verifies private ref after reset but before Builder launch; B2 handles automatic import refusal via #990 and B0 snapshot.
- No Manager dispatch, proof/adoption, harvest, D gates or push.
- See the [descendant issue drafts](../../../../issue-backed-descendants.md).

## Tasks

- [ ] T1 Validate run/C/M/classification/path set and Candidate C CAS from same-run context before reset.
- [ ] T2 Validate B1a Manager source pin and compute deterministic Builder private-ref name from run/C/M/classification/paths/next build ordinal; do not require per-job clone ref to exist yet.
- [ ] T3 Bind C/M, expected private ref, full classification and automatic reservation id into shared task construction.
- [ ] T4 Ensure B1b import verification gates Builder launch; automatic provisioning failure uses B0 snapshot and #990 without refund.
- [ ] T5 Specify true ordered-parent merge [C,M], exact CHANGELOG entry retention/order, other clean M paths and complete stop paths.
- [ ] T6 Test manual/automatic helper parity, pre-reset absent private ref, post-reset exact import/failure, fixed M through M→N and no fallback; reserve real Git proof for B3.

## Sizing

One production module, existing task/action state only: domain_breadth=0/state_consistency=1, complete fix-standard score **5 / Yellow**.

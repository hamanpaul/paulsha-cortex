---
status: accepted
work_item: quarantined-harvest-ref-cas
domain_breadth: 0
state_consistency: 1
invariant_count: 4
artifact_classes:
  - source
  - tests
  - documentation
---

# Quarantined harvest + expected-ref CAS（#973 Child B4）

## Boundary

- Scope: job_workspace.py Git bundle import and explicit expected-old ref promotion API.
- B3 owns Manager proof and calls this API only after quarantine proof succeeds.
- This child does not adopt Candidate, change workflow state or interpret #972 policy.
- Preserve legacy harvest_branch for other callers; do not use it as B3's first import/promotion operation.
- See the [descendant issue drafts](../../../../issue-backed-descendants.md).

## Tasks

- [ ] T1 Add exact advertised bundle import to a unique quarantine ref; prove feature refs remain untouched.
- [ ] T2 Add branch promotion API backed by atomic `git update-ref <target> D C` and validate both OIDs.
- [ ] T3 Reject C→E→D race where E is ancestor of D; leave E unchanged despite ordinary non-FF acceptance.
- [ ] T4 Permit already-D only with matching B3 proof identity; reject unrelated movement and force updates.
- [ ] T5 Use bare-origin real Git tests for import, success, CAS race, wrong C, missing D and idempotent replay.

## Sizing

One production module, one quarantine/ref transaction: domain_breadth=0/state_consistency=1. Complete fix-standard score is 5 / Yellow.

---
status: accepted
work_item: main-sync-builder-m-object-import
domain_breadth: 0
state_consistency: 1
invariant_count: 4
artifact_classes:
  - source
  - tests
  - documentation
---

# Builder clone exact-M import（#973 B1b）

## Boundary

- Scope: seams.py ScriptWorktreeCreator clone provisioning only.
- Dependencies: B1a provides the stable attempt-scoped exact-M source pin; B1c independently computes the same deterministic private-ref name before reset. B1b imports/verifies the private ref after reset and before launch; it does not depend on B1c implementation being merged.
- Create private `refs/cortex/main-sync/<token>` in the new clone; preserve checked-out branch/base C and never fetch main.
- No Builder merge or Manager Candidate adoption here.
- See the [descendant issue drafts](../../../../issue-backed-descendants.md).

## Tasks

- [ ] T1 Extend creator handoff with pre-reset run/C/M/attempt-token/target-ordinal identity; bind actual job ID only after provisioning allocates it.
- [ ] T2 During clone setup, explicitly fetch the exact pin from the local Manager source path into the job-scoped private ref and verify commit SHA before Builder launch.
- [ ] T3 Assert HEAD, feature branch and existing base ref remain C; do not alter origin/main or fetch from network/main.
- [ ] T4 Roll back clone/private ref on missing, wrong, stale or ambiguous pin and withhold B1a acknowledgement.
- [ ] T5 Test Builder source clone lacks M while Manager source pin carries it; test main advances M→N and exact M remains local.

## Sizing

One production module and one clone-local ref yield domain_breadth=0/state_consistency=1. Complete accepted fix-standard artifacts score 5 / Yellow.

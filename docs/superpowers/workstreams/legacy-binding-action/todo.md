---
status: accepted
work_item: legacy-binding-action
issue: TBD
domain_breadth: 1
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# I — explicit migration action Tasks

## Tasks

- [ ] **T01**：Add and route the explicit command/action through Manager.
- [ ] **T02**：Require H principal and exact WorkAuthority; present exact before-image fingerprints for confirmation.
- [ ] **T03**：Verify exact persisted slice/job/WorkflowRun lineage and stop before proof when missing or foreign.
- [ ] **T04**：Call G for the exact workspace, freeze immutable request payload and call J once.
- [ ] **T05**：Implement receipt-first replay, genuine history positive fixture, same-name/unbound/auth/stale negatives and zero-side-effect assertions.
- [ ] **T06**：Update help/docs/changelog and run focused/full/policy gates; keep blocked if evidence is unavailable.

## Sizing and completion record

Five dimensions: verified current_sizing_snapshot output = 6 / Yellow (1/1/2/0/2) under fix-standard. Artifact refs are the sibling spec, design and todo. Record implementation/test/review/CI/merge/runtime evidence separately.

## Non-goals

No registry writer, helper/runner implementation, auth policy implementation, shared recovery core, automatic resolver, or synthetic owner assignment.

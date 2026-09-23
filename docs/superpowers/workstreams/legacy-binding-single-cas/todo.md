---
status: accepted
work_item: legacy-binding-single-cas
issue: TBD
domain_breadth: 0
state_consistency: 2
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
---

# J — existing writer CAS extension Tasks

## Tasks

- [ ] **T01**：Get E and #862 owner acceptance before touching registry writer.
- [ ] **T02**：Implement exact same-transaction scan/reselect for versioned and legacy rows, job and WorkflowRun.
- [ ] **T03**：Validate fingerprints, identity tuple, immutable lineage, proof digest and revision before first mutation.
- [ ] **T04**：Persist slice/job identities, migration record and existing complete receipt in one durable write; preserve rollback semantics.
- [ ] **T05**：Test foreign same-name, zero/multi target, changed fingerprints, proof mismatch, reload, replay, write and rollback failures.
- [ ] **T06**：Run focused/full/policy tests and stop if #862 contract requires a new writer/receipt.

## Sizing and completion record

Five dimensions: verified current_sizing_snapshot output = 6 / Yellow (0/2/2/0/2) under fix-standard. Artifact refs are the sibling spec, design and todo. Record implementation/test/review/CI/merge/runtime evidence separately.

## Non-goals

No new checkpoint service, prepared phase, proof helper/runner, operator authorization, CLI action, resolver, recovery core or workspace cleanup.

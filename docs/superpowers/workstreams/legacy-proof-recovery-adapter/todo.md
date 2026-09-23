---
status: accepted
work_item: legacy-proof-recovery-adapter
issue: TBD
domain_breadth: 1
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# K — markerless recovery adapter Tasks

## Tasks

- [ ] **T01**：Obtain #970 owner acceptance and define adapter input/output before code.
- [ ] **T02**：Read and validate J-created complete record/receipt and exact durable identities.
- [ ] **T03**：Request fresh G proof for the recorded exact workspace; reject marker/path/origin drift.
- [ ] **T04**：Pass verified target to shared #970 core without duplicating selection, reclaim, state or handoff behavior.
- [ ] **T05**：Test genuine historical positive and unbound/foreign/missing/stale zero-effect negatives.
- [ ] **T06**：Run separate-UID proof and repo gates; keep parent AC7 open if no eligible historical evidence exists.

## Sizing and completion record

Five dimensions: verified current_sizing_snapshot output = 6 / Yellow (1/1/2/0/2) under fix-standard. Artifact refs are the sibling spec, design and todo. Record implementation/test/review/CI/merge/runtime evidence separately.

## Non-goals

No migration action, identity writer, auth policy, checkpoint implementation, proof helper/runner, duplicated recovery state machine, terminal replay fix or retire-delivered cleanup.

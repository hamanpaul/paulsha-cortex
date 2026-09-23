---
status: accepted
work_item: legacy-proof-runner-transport
issue: TBD
domain_breadth: 1
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# G — fixed Builder runner Tasks

## Tasks

- [ ] **T01**：Verify existing install-service control path and permissions before implementation.
- [ ] **T02**：Install a fixed root-owned broker/unit and strict request/result contracts.
- [ ] **T03**：Constrain execution to cortex-builder and exact read-only workspace; bind transcript to invocation ID and nonce.
- [ ] **T04**：Add lifecycle, tamper, replay and denial tests.
- [ ] **T05**：Run real separate-UID acceptance with the private clone and preserve Manager access denial.

## Sizing and completion record

Five dimensions: verified current_sizing_snapshot output = 6 / Yellow (1/1/2/0/2) under fix-standard. Artifact refs are the sibling spec, design and todo. Record implementation/test/review/CI/merge/runtime evidence separately.

## Non-goals

No proof schema ownership (F), WorkAuthority/OS operator authorization (H), registry transaction, identity migration, recovery adapter or cleanup policy.

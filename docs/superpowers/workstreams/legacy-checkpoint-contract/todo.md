---
status: accepted
work_item: legacy-checkpoint-contract
issue: TBD
domain_breadth: 0
state_consistency: 1
invariant_count: 5
artifact_classes:
  - documentation
---

# E — checkpoint contract Tasks

## Tasks

- [ ] **T01**：Obtain #862 owner acceptance and write exact contract reference.
- [ ] **T02**：Freeze the versioned J input payload schema in the sibling spec, including digest inputs, exact fingerprints/cardinality and historical lineage predicate.
- [ ] **T03**：Specify typed proof and operator provenance, digest ordering, receipt-first replay, and fail-closed errors.
- [ ] **T04**：Assign helper, runner, Manager verifier, caller and registry writer ownership; J depends on E's payload contract and I consumes J without a dependency cycle. If #862 owner rejects, record blocked and stop downstream work.
- [ ] **T05**：Review the contract against #862 I09 and the #547 proposed AC7 without asserting live implementation support.

## Sizing and completion record

Five dimensions: verified current_sizing_snapshot output = 5 / Yellow (0/1/2/0/2) under fix-standard. Artifact refs are the sibling spec, design and todo. Record implementation/test/review/CI/merge/runtime evidence separately.

## Non-goals

No product code, registry mutation, proof generation, operator auth, migration action, or legacy row conversion. No change to proposal-first gc.

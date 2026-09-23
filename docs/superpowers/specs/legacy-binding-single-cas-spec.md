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

# J — existing writer CAS extension 規格

## Authority and scope

Extend only #862's existing registry checkpoint writer so exact target reselect, historical lineage validation, identity/migration record and existing complete receipt commit atomically.
Blocked by E/#862 owner acceptance, #968 A1 and F typed proof contract. J consumes the canonical payload schema frozen in E; I implements the caller that emits that schema and consumes J. J does not depend on I.

## Requirements

- **R1** — Reuse the existing #862 writer, CAS/revision, rollback and receipt type. No prepared receipt, second writer, second transaction or new receipt kind.
- **R2** — Within the same transaction scan/reselect exact slice, job and WorkflowRun by complete fingerprints and cardinality; revalidate repo/work_id lineage. Never trust external prefilter or slice_id first-match.
- **R3** — Accept F/G typed proof and H/I operator provenance only as immutable request inputs; proof, observed_at and full request digest are fixed before the call and never refreshed inside transaction.
- **R4** — Success atomically writes slice/job identity tuples, durable migration record and existing complete receipt; fresh reload agrees.
- **R5** — Zero/multiple targets, same-name foreign rows, lineage/proof/fingerprint/revision drift, partial tuple, persist/rollback failure leave no partial mutation; rollback failure is fatal.
- **R6** — Receipt-first replay returns the original complete receipt before new proof; reused request ID with changed content conflicts and is not automatically rehashed.
- **R7** — Only rows with existing exact immutable lineage can migrate; otherwise remain legacy_unbound/needs_human.

## Acceptance and verification

- [ ] Reuse the existing #862 writer, CAS/revision, rollback and receipt type. No prepared receipt, second writer, second transaction or new receipt kind.
- [ ] Within the same transaction scan/reselect exact slice, job and WorkflowRun by complete fingerprints and cardinality; revalidate repo/work_id lineage. Never trust external prefilter or slice_id first-match.
- [ ] Accept F/G typed proof and H/I operator provenance only as immutable request inputs; proof, observed_at and full request digest are fixed before the call and never refreshed inside transaction.
- [ ] Success atomically writes slice/job identity tuples, durable migration record and existing complete receipt; fresh reload agrees.
- [ ] Zero/multiple targets, same-name foreign rows, lineage/proof/fingerprint/revision drift, partial tuple, persist/rollback failure leave no partial mutation; rollback failure is fatal.
- [ ] Receipt-first replay returns the original complete receipt before new proof; reused request ID with changed content conflicts and is not automatically rehashed.
- [ ] Only rows with existing exact immutable lineage can migrate; otherwise remain legacy_unbound/needs_human.

## Non-goals

No new checkpoint service, prepared phase, proof helper/runner, operator authorization, CLI action, resolver, recovery core or workspace cleanup.

## Verification boundary

此三件套 status=accepted 只代表 planning scope 已寫完整，不代表外部 issue owner acceptance、產品實作、測試、CI、merge 或 #547 closure。

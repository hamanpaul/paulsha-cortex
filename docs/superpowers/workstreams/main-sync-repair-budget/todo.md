---
status: accepted
work_item: main-sync-repair-budget
domain_breadth: 0
state_consistency: 2
invariant_count: 5
artifact_classes:
  - source
  - tests
  - documentation
---

# Main-sync automatic repair budget（#973 Child B0）

## Boundary

- #972 supplies typed C/M/classification, but no numeric counter; #990 supplies the only durable needs-human stop writer. Hard dependency: #966 whole-registry revision CAS merged.
- This child is the sole budget owner in `registry.py`; use existing WorkflowRun `attempts: dict[str,int]`, `evidence_refs`, and coordinator evidence storage; derive remaining.
- Limit is one automatic dispatch reservation per exact `(run_id,C,M,repair_kind,canonical conflict paths)` tuple. Manual operator retry remains under #989 and is not charged here.
- Reservation ID is deterministic from tuple digest and ordinal 1; no schema/string field is added.
- See the [descendant issue drafts](../../../../issue-backed-descendants.md).

## Tasks

- [ ] T1 Define deterministic versioned tuple key; persist `limit=1` and `used` as separate integer entries in existing attempts; derive remaining and reject malformed/changed values.
- [ ] T2 Create/fsync immutable full typed C/M/classification/path/budget snapshot before reset; add registry reserve/reset operation that validates context and preconditions.
- [ ] T3 Under #966 CAS, persist snapshot locator+digest, used increment, deterministic-key pending action and final Builder-card reset in one atomic WorkflowRun state write immediately before dispatch authorization.
- [ ] T4 Prove CAS conflict is not a reservation: reload state, leave counter unchanged, and dispatch nothing. On committed re-entry, load/verify snapshot and recompute the same reservation ID; reuse/deduplicate the same job or fail closed.
- [ ] T5 Prove reset refusal/persistence failure does not charge; exhaustion does not reset/dispatch and lets B2 call #990's stop writer.
- [ ] T6 Test one-winner #966 concurrent reservation, replay after needs_human_reason is cleared, tuple isolation, M→N new tuple, duplicate/mismatched job token, and #989 manual retry non-interference.

## Sizing

One production module gives domain_breadth=0. WorkflowRun durable count, reset and dispatch-reservation replay require state_consistency=2. Accepted fix-standard mechanics yield **6 / Yellow**.

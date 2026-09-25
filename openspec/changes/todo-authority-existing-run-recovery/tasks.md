---
status: draft
work_item: todo-authority-existing-run-recovery
issue: 1055
domain_breadth: 2
state_consistency: 2
invariant_count: 10
artifact_classes:
  - source
  - tests
  - documentation
---

## Tasks

- [ ] T0 Verify merged #966, #1054 and #1063/#1064/#1065 accepted contracts; freeze exact main revisions and named owners for #1068/#1069/#1070.
- [ ] T1 Verify #983 accepted conditional journal writer and exact read-back contract on main; do not use PR #1049 as source authority.
- [ ] T2 Freeze the exact registry/run/old-new WorkAuthority/Candidate/PR/job/evidence/journal CAS tuple and conflict result.
- [ ] T3 Implement #1068 one-module registry exact-run CAS primitive; stale/conflicting/replayed requests preserve exact durable state and historic bindings.
- [ ] T4 Implement #1069 explicit Manager same-run authority restart, invalidating verify/review only and revalidating the unchanged Candidate before each new gate job.
- [ ] T5 Implement #1070 existing-PR journal idempotent read-back using #983; no repeated push, PR creation, merge, close or false closure.
- [ ] T6 Add fixture-only regression based on #983 run workflow-52d048b72adbd5cae06f, Candidate 7ba7e877c94ff4eee72ba796ea9f8962953ed5cc and PR #1049. Include all missing/ambiguous/stale/digest drift, CAS mismatch, crash/re-entry and repeated-resume negatives; assert no duplicate side effect.
- [ ] T7 Keep #983 run and PR #1049 read-only; report fixture-only evidence separately.
- [ ] T8 Add lifecycle docs, committed changelog fragment and CHANGELOG entry; do not change VERSION.
- [ ] T9 Run the required focused/full tests for the implementation plus strict OpenSpec and PR-context policy. This Draft planning PR runs no pytest unless specifically requested.
- [ ] T10 Recompute official five-dimensional sizing whenever accepted inputs, code modules, tasks or boundaries change; preserve full parent acceptance and keep #1055 open until integrated regression passes.

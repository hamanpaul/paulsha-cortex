---
status: accepted
work_item: producer-lineage-resolver
domain_breadth: 1
state_consistency: 1
invariant_count: 5
artifact_classes:
  - source
  - tests
  - documentation
---

# Producer lineage resolver（#973 Child A）

## Boundary

- Scope: manager.py shared resolver, retry selectors and dispatch provenance; work_bridge.py ship binding selector.
- Dependency: #972 must be merged before auto-repair integration; no auto-retry is enabled by this child.
- #885 owns archive apply/Aborted/tree-coexistence behavior; this work consumes its existing successful archive evidence only.
- This child owns all selector wiring and post-archive C metadata correction.

## Tasks

- [ ] T1 Enumerate the exact WorkflowRun, archive job/evidence, B Builder job/evidence and Candidate C Git object fields consumed.
- [ ] T2 Implement one read-only resolver for direct Candidate producer and archive C lineage; return typed unresolved outcomes on missing, duplicate, mismatched or invalid evidence.
- [ ] T3 Use Git object ancestry to prove C^1 == B; never infer it from summaries.
- [ ] T4 Prove the B job is lineage-only and cannot be returned as current C/D producer.
- [ ] T5 Test success, malformed, stale-era, ambiguous and mutation-trap cases with a real Git fixture.
- [ ] T6 Wire manager.resume_workflow_run jobs[-1], _dispatch_workflow_card reusable selection and work_bridge._builder_binding to the shared resolver; do not count _review_builder_job_binding as a retry selector.
- [ ] T7 Ensure post-archive clone base and immutable Builder job dispatch_head are exact C, not B copied from historical Builder. Treat subject_head as successful output/result evidence (absent before job output, D after success); bind initial C/M in B1 task input.
- [ ] T8 Recheck #765 behavior and prove current-era exact-C crash-window reuse plus fresh exact-C dispatch.

## Conditional sizing

fix-standard combo and accepted artifacts yield acceptance=2, stability=0, orchestration=2. Two modules yield domain=1; the shared resolver reads multiple records while provenance correction uses the existing job recorder and adds no transition/schema (state=1). Expected score: 6 / Yellow.

---
status: accepted
work_item: d-bound-ship-gates
domain_breadth: 1
state_consistency: 2
invariant_count: 5
artifact_classes:
  - source
  - tests
  - documentation
---

# D-bound gates 與 staged ship（#973 Child C）

## Boundary

- This is the complete D-bound delivery aggregate C, not an implementation dispatch slice. Accepted Yellow descendants C1 and C2 own its implementation scopes; C is blocked by those children and does not block them.
- Scope: manager.py/work_bridge.py existing Candidate-bound gates and staged delivery.
- Dependencies: #972 merged, plus #973 B2 automatic retry/stop and B3 verified Candidate adoption.
- Stage order: local exact-D gates and preflight before push; exact-D push and PR create/update; exact-D PR CI after push before merge/closure.
- Reuse existing delivery journal, PR identity and existing push/merge/closure authority. No new schema or authority.
- Full parent and umbrella acceptance remains in #943/#973; see the [child issue drafts](../../../../issue-backed-descendants.md).

## Tasks

- [ ] T1 Prove every C-bound build/verify/test/review/maintainer-review/preflight/CI result is stale after Candidate C→D; #847 shortcut does not apply.
- [ ] T2 Obtain authoritative build harvest, verify/tests, cross-domain review and required maintainer review with exact subject D.
- [ ] T3 Run local exact-head preflight on D; assert target and evidence bind to D.
- [ ] T4 Stop on missing, failed, stale, wrong-head or ambiguous local evidence before feature push or PR mutation.
- [ ] T5 After local gates pass, use the existing journaled path to push exact D; reconcile push outcome before further remote effects.
- [ ] T6 Create or update/reconcile the authorized PR after push; reuse an existing mapped PR and assert its head is D.
- [ ] T7 Read post-push required PR CI for exact head D; pending/fail/missing/stale/wrong-head/ambiguous blocks merge, issue closure and run completion.
- [ ] T8 Preserve existing authority and resumable not-mergeable/pending path; test interruption/re-entry without duplicate push/PR or stale-SHA merge.

## Sizing

Actual declared dimensions: domain_breadth=1 (manager.py and work_bridge.py), state_consistency=2 (delivery journal, Git push, PR identity/head, remote CI and lifecycle closeout reconcile across restart), acceptance=2, stability=0 for accepted complete artifacts, orchestration=2. Total 7 / Red. Keep this aggregate score honest; C1/C2 accepted descendants are the Yellow implementation intake units.

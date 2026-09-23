---
status: accepted
work_item: ship-main-trusted-merge-candidate
domain_breadth: 2
state_consistency: 2
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
---

# #973 trusted main merge candidate umbrella

## Boundary

- Issue #973 is Child B of #943 and blocked by #972.
- #972's live #987→#988→#989→#990 chain must merge before runtime integration. It supplies C/M/classification and recovery/stop APIs, but no numeric retry counter.
- B0 owns one durable automatic reservation per exact tuple in existing WorkflowRun `attempts`; #990 remains the only durable `needs_human` stop writer.
- Full acceptance umbrella only. B1 and C remain Red aggregates; implement through the issue-backed Yellow descendants in `issue-backed-descendants.md`.
- #885 owns archive apply/Aborted and active/archive coexistence behavior; excluded here. The revised #943 draft's older fail-open probe wording is a parent-owner handoff.
- Preserve the complete #943 and #973 acceptance; do not report this umbrella as Yellow.

## Tasks

- [ ] T1 Merge #972 chain; verify exact C/M/classification, fail-closed read-back, recovery actions and #990 writer. Merge #966 whole-registry revision CAS before B0. Verify B0 separately owns numeric `limit=1`, durable `used`, derived `remaining`, and the immutable typed snapshot needed after retry-build clears the diagnostic.
- [ ] T2 Complete A: lineage proof, actual `jobs[-1]` and reusable-job selectors, `_builder_binding`, and new post-archive dispatch `dispatch_head=C`.
- [ ] T3 Complete B0 and B1a→(B1b || B1c): atomically reserve/reset under #966; pin exact M; construct task/ref name before reset; after reset B1b imports/verifies M before Builder launch. B1b and B1c are independent after B1a.
- [ ] T4 Complete B2 automatic caller and durable refusal; serialize its `manager.py` merge after A/B1a and before B3.
- [ ] T5 Complete B3 proof from quarantine and B4 expected-old ref CAS before Candidate C→D adoption; test concurrent C→E refusal.
- [ ] T6 Complete C1 local exact-D gates, then C2 exact-D push, witnessed PR reconciliation, post-push exact-D PR CI and existing-authority closeout. C2 requires #980/#982 only for new-PR create/adopt; existing mapped PR C→D update has C2-owned durable intent/result and authenticated same-PR read-back.
- [ ] T7 Run full #973 and #943 integration acceptance; keep parent issues open until real-Git and lifecycle evidence is complete.
- [ ] T8 Keep #885 boundary and #943 fail-open wording as owner handoffs only.

## Merge order and sizing

B0 is hard-blocked on #966 whole-registry revision CAS; a CAS conflict is not a reservation and never dispatches. Same-file `manager.py` changes are serial: **A → B1a → B2 → B3 → C1 → C2 integration**. B1b (`seams.py`) and B1c (`work_actions.py`) independently follow B1a and both land before B2. B0 (`registry.py`) and B4 (`job_workspace.py`) may otherwise proceed independently; B2 waits for B0 and all B1 children, B3 waits for B4, and C2 also waits for #980/#982 only on its new-PR create/adopt path. Existing mapped PR C→D update has a C2-owned durable intent/result and authenticated read-back. C1/C2 do not depend on the C aggregate issue.

The full six-module scope gives domain_breadth=2 and state_consistency=2. With accepted complete artifacts and fix-standard mechanics, score is **8 / Red**; issue-backed children own implementation.

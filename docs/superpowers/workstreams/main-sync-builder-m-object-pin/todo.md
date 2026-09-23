---
status: accepted
work_item: main-sync-builder-m-object-pin
domain_breadth: 0
state_consistency: 1
invariant_count: 5
artifact_classes:
  - source
  - tests
  - documentation
---

# Manager-side exact-M object pin（#973 B1a）

## Boundary

- Scope: manager.py exact-M source-ref publication and pre-reset handoff.
- Dependency: #972 typed C/M context; serialize manager.py owner after A and before B2.
- Derive pin token from run/C/M/classification/paths/next build-attempt ordinal only; job/reservation IDs do not exist pre-reset.
- B1b imports after reset and before launch, then binds actual job id; B1c builds the task using the same expected ref name.
- Never fetch floating main, move feature/main refs or substitute M.
- See the [descendant issue drafts](../../../../issue-backed-descendants.md).

## Tasks

- [ ] T1 Resolve M as a commit in Manager source clone and bind same-run Candidate C/context; absent M stops before reset/dispatch.
- [ ] T2 Read next build-attempt ordinal and derive stable attempt token/ref name from run/C/M/classification/paths/ordinal, excluding job and reservation IDs.
- [ ] T3 Create a deterministic advertised source ref pointing exactly M; re-entry accepts only same value; bind token/ordinal in pending task.
- [ ] T4 Keep pin until B1b exact-M import receipt binds actual job id; then remove only exact matching ref.
- [ ] T5 Test same manual/automatic token, reset/CAS refusal, next ordinal, missing M, conflicting ref, clone failure/ack and M→N while all main/feature refs remain unchanged.

## Sizing

One module and one scoped temporary Git ref give domain_breadth=0/state_consistency=1. Complete accepted fix-standard triad scores **5 / Yellow**.

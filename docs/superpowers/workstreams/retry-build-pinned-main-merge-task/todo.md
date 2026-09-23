---
status: accepted
work_item: retry-build-pinned-main-merge-task
domain_breadth: 1
state_consistency: 2
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# 固定 C/M task/transport aggregate（#973 Child B1）

## Boundary

- This is a complete Red aggregate, not an implementation dispatch card. Its Yellow children are B1a (Manager exact-M pin), B1b (Builder clone import), B1c (shared task helper).
- Runtime integration waits for #972 chain #987→#988→#989→#990. A→B1a→B2→B3→C1 are serialized owners of manager.py. B1b/seams.py and B1c/work_actions.py may proceed after B1a independently; both complete before B2. B1c constructs the deterministic private-ref name before reset; B1b imports/verifies M after reset and before Builder launch.
- Manager source clone owns M; Builder clone receives M through an explicit local-path fetch to a private ref. Task construction precedes clone provisioning; B1b verifies the private ref before launch and never fetches floating main.
- B3/B4 own true Git proof, quarantine and expected-ref CAS; B1 does not adopt D or deliver it.
- See [issue-backed descendants](../../../../issue-backed-descendants.md).

## Tasks

- [ ] T1 Validate same-run C/M/classification/paths and Candidate C CAS before any reset/dispatch.
- [ ] T2 Pin exact M in a per-job Manager source ref without changing feature/main (B1a).
- [ ] T3 Build the shared manual/automatic task before reset with exact C/M and deterministic target private-ref name; do not require a Builder clone/ref yet (B1c).
- [ ] T4 After B0 reset, explicitly transfer exact M into Builder clone and verify the private ref before launch (B1b); on auto failure B2 writes #990 stop from B0 snapshot without refund.
- [ ] T5 Keep true `[C,M]` merge and full CHANGELOG/other M tree requirements; other conflicts stop.
- [ ] T6 Preserve B1's task-only boundary and verify dependencies/owner order through B2/B3/C.

## Sizing

Three production modules (`manager.py`, `seams.py`, `work_actions.py`) give domain_breadth=1; cross-clone M object and dispatch/task agreement gives state_consistency=2. Accepted fix-standard score is 7 / Red. The three child issues score independently Yellow.

---
status: accepted
work_item: merged-verify-registry-transition
domain_breadth: 0
state_consistency: 2
invariant_count: 10
artifact_classes:
  - source
  - tests
  - documentation
---

# verify-reset merged run 的受限 registry terminal transition Todo（#976）

## Scope and dependency

- Issue: [hamanpaul/paulsha-cortex#976](https://github.com/hamanpaul/paulsha-cortex/issues/976); parent #962; grandparent #887.
- Accepted authority: [spec](../../specs/merged-verify-registry-transition-spec.md) and [design](../../specs/merged-verify-registry-transition-design.md).
- Dependency order is #961 → #975 → #976 → #977. #976 implementation/integration waits until #975 merges with the proof binding contract frozen. #977 is blocked by #975 and #976.
- #976 owns only a private JobRegistry transition in paulsha_cortex/coordinator/registry.py and registry-level tests. It consumes a Manager-provided expected authorization hash and complete terminal binding; it does not produce or verify external proof.
- #975 owns admission and proof oracle. #977 owns Manager finalizer, CompletionRecord/outcome writing and completion crash/retry. #962/#887 retain their full acceptance and close only after the three slices and integration gates land.
- Keep all ten #976 acceptance criteria. Do not widen _manager_update_workflow_run or the common phase validator to make verify→ship generally legal.

## Repository evidence at intake

- Initial source inspection used main HEAD a24565b8. The shared checkout later advanced to e657650b (`docs(refine): 進件 #966 registry CAS 規劃 (#981)`); that intervening commit changes planning docs only, with no diff in the #976 source/sizing modules. Preserve eight unrelated untracked documents under docs/superpowers/plans/; implementation must use an isolated, policy-compliant feature worktree rather than this shared main checkout.
- No restricted merged-verify registry method or dedicated #976 test module exists in the current source.
- registry.py::_manager_update_workflow_run() calls validate_workflow_phase_transition(), whose rule permits only same phase or the next phase; verify→ship is currently rejected.
- WorkflowRun.__post_init__ already requires complete done fields and, for ship, passed gates, exactly one current-HEAD delivery review, verified_head equal to candidate_head, verify/review/ship steps all passed, and reviewer/builder domain separation. Preserve these checks.
- registry.py::ACTIVE_JOB_STATUSES is exactly dispatched/running. The new method must check jobs for the exact workflow_run_id within the same synchronous mutation.
- tests/test_workflow_registry.py already covers typed persisted updates and rejects phase regression. Keep its original assertions; add focused cases in a separate test module.

## Five-dimension sizing

Scope is the #976 registry child after all three planning artifacts are accepted:

| Dimension | Score | Basis |
|---|---:|---|
| domain_breadth | 0 | The sole production module is coordinator/registry.py. |
| state_consistency | 2 | Exact run/head/authorization/terminal-field binding, active-job gate, first-transition stamp, one terminal-row persist, rollback and no-write same-value re-entry preserving `updated_at`; no origin marker or provenance inference. |
| acceptance_surfaces | 2 | Current sizing adapter feeds R-09/R-16/R-19; fix-standard has 2 gate_spine entries, so signal=2+3=5 and score=2. |
| spec_stability | 0 | Complete accepted spec/design/plan with no blocking markers under stability-risk-v2. |
| orchestration | 2 | fix-standard has 9 cards and 9 persona bindings. |

Projected score: 6 / Yellow. This matches the live issue projection. The sizing helper yields spec_stability risk 2 (total 8 / Red) before the accepted triad exists, so do not dispatch an implementation from issue text alone. Recompute with the currently linked combo before implementation; any second production module, persisted schema change, or proof/finalizer scope invalidates this projection and requires re-sizing/splitting.

## Tasks

- [ ] **T1 tests / RED — registry transition contract (R8; AC 1–4, 8–10)**: Add tests/test_registry_merged_verify_transition_976.py with local builders for an ongoing verify/authority_restart run and complete steps/gates/binding. No GitHub, provider, journal, authorization-file or Manager-finalizer calls. Assert the proposed private API is the only special route and common phase validator/general update behavior remains unchanged.
- [ ] **T2 source / private API and binding validation (R1–R3; AC 1–3, 8)**: In registry.py add _manager_complete_merged_verify_reset_workflow_run(run_id, *, expected_candidate_head, authorization_hash, terminal_binding). Enforce exact required/allowed keys, strict hash/SHA/map/path/types, identity match, and proof-hash versus validated-record authorization-hash equality. For first transition require the exact ongoing + verify + authority_restart + expected candidate preconditions; handle an existing terminal row only through T4's exact no-write path. Reject missing, malformed, mismatched or partial binding before memory/file mutation.
- [ ] **T3 source / active-job and ship invariants (R4–R5; AC 3–5)**: In the same synchronous method check self._jobs for this exact run's dispatched/running job. Construct the restricted ship/done WorkflowRun only after every check passes; carry only supplied Manager-verified steps/gate refs/CompletionRecord binding into existing fields. Preserve run identity, attempts, candidate, authority and retry classification. Let WorkflowRun validation enforce existing ship/done invariants; do not weaken common phase rules or add schema.
- [ ] **T4 source / exact no-write re-entry and failure atomicity (R6–R7; AC 6–7)**: If an already ship/done authority_restart run has exact identity/head/auth/record/source-revision/gates/steps/facets terminal equality after the trusted Manager caller revalidates #975 proof and the CompletionRecord binding, return the current copy without _persist and preserve its existing updated_at. Reject any changed terminal field or auth-vs-record mismatch without overwrite. The current schema has no origin marker, so do not claim or test writer-origin inference; an ordinary-written row with identical fields is indistinguishable and follows the same no-op equality rule. On first transition set updated_at once. Build the new row before assignment; if _persist raises, restore the prior in-memory workflow row and re-raise. Test durable rollback using the existing atomic writer seam and test a pre-write _persist exception leaves raw jobs.json and run/job snapshots unchanged.
- [ ] **T5 tests / exact positive and negative matrix (AC 2–9)**: Cover valid persisted transition/readback; wrong phase/status/retry/run/work/repo/head/auth; incomplete and malformed CompletionRecord binding; active job; incomplete/wrong-type gates and steps; ship invariant violations; same-value no-persist re-entry; changed done binding rejection; persist failure and expected-state drift. Confirm ordinary verify, build and review runs cannot use this API to terminate.
- [ ] **T6 compatibility, docs and changelog (AC 1, 9–10; policy surfaces)**: Update docs/unified-work-lifecycle.md to document this narrow recovery transition and state that general phase progression remains unchanged. Add the implementation branch changelog.d fragment and CHANGELOG.md [Unreleased] entry per repo policy. Do not add a public API or CLI.
- [ ] **T7 focused and required verification (all AC)**: Run the new registry test module, tests/test_workflow_registry.py and affected registry tests, then full pytest. Run policy_check with actual PR title/body/labels/base/head context. Record exact final lines and any skipped/limited gate. Do not claim #975 proof or #977 finalizer integration from these unit tests.
- [ ] **T8 downstream handoff (R9)**: After #976 lands, provide #977 the exact method signature and full terminal_binding contract. #977 must revalidate #975 proof and CompletionRecord on every call; derive `completion_source_revisions` from the validated record with the existing `rsplit("@", 1)` projection, and use retry-time WorkAuthority only for proof/identity validation, not to replace the frozen map. Keep this caller adapter in #977; do not add a Manager call site here. Leave #962/#887 open for R8(a)–(d) integration.

## #976 acceptance traceability

| Live #976 acceptance | Plan evidence |
|---|---|
| Private Manager-only transition only in registry.py; common updater/validator unchanged | T2–T3 plus T1 general API/phase regression |
| Exact run/head/auth hash/full terminal binding required | T2 strict binding field/type/equality validation |
| First write requires ongoing verify authority_restart with exact candidate; existing terminal state only allows exact no-write R6 replay | T2 preconditions and T4 equality path |
| Same synchronous mutation rejects dispatched/running job | T3 active-job check before row construction/persist |
| One valid ship/done WorkflowRun with only proven fields and existing schema | T3 constructor invariants/readback |
| Trusted revalidated same terminal fields are idempotent without persist and preserve updated_at; different values reject, with no writer-origin claim | T4 exact equality, timestamp and mismatch tests |
| Persist failure or expected-state drift leaves no partial memory/durable mutation | T4 failure injection and raw-state/run/job comparison |
| Positive, malformed binding, transition, active-job and replay cases without GitHub network | T1/T5 registry-only fixtures |
| General verify/build/review runs still cannot terminalize | T1/T5 negative coverage and existing tests |
| No CLI; #962 finalizer calls later and runs outcome-first integration | T6/T8 scope handoff |

## #887 parent acceptance boundary

- This slice contributes only the registry portion of parent R3/R6 and R8(a)–(d). #975/#977 and #961 own the other listed behavior as shown in the accepted spec.
- Do not mark parent R1/R2/R4/R5/R7 complete from this API. Do not replace #962 end-to-end integration tests with registry unit tests.
- Successful #976 means the narrow primitive is ready for #977 to call; #977 must revalidate the #975 proof and CompletionRecord each time and build the exact binding from normalized record plus trusted writer/closure metadata using the D2 source-revision projection. It does not mean a merged run has been recovered or #962/#887 can close.

## Non-goals

- No changes to manager.py, work_actions.py, workflow.py, proof oracle, completion record writer/validator, outcome store or delivery journal.
- No generic verify→ship/done transition, public method, CLI, new WorkflowRun/Job persisted field or schema version.
- No GitHub-network tests, end-to-end Manager completion, external evidence validation or claim authority arbitration.
- No edits to GitHub/Cortex, this repository checkout, unrelated untracked planning files or dependent issue bodies.

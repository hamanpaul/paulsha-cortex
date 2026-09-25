---
status: accepted
work_item: registry-revision-cas
domain_breadth: 0
state_consistency: 2
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
---

# JobRegistry revision CAS Todo（#966）

## Scope and dependency

- Issue: [hamanpaul/paulsha-cortex#966](https://github.com/hamanpaul/paulsha-cortex/issues/966); parent: [#818](https://github.com/hamanpaul/paulsha-cortex/issues/818).
- Accepted authority: [spec](../../specs/registry-revision-cas-spec.md) and [design](../../specs/registry-revision-cas-design.md).
- #966 owns JobRegistry exact raw-byte revision CAS, per-persist transaction lock, conflict exception and memory restore, migration/normalization CAS coverage, Manager request error acknowledgement, and cross-process tests.
- #967 is a separate dependent child. It owns Manager lifetime writer ownership, startup rejection and owner diagnostics, plus complete live Manager collision inventory in doctor. Do not add its owner lock or /proc inventory to this implementation. #818 stays open until both children and its owner/risk acceptance pass.
- No upstream child must merge before #966. #967 is blocked by #966.
- Keep the full #966 scope and all eight acceptance criteria. Do not substitute reload/merge/retry for exact CAS or turn a failed mutation into success.

## Repository evidence at intake

- Current checkout observed at main HEAD ea3f81eff45be532d7f99155399b02d4e37be1a0. No RegistryRevisionConflict, transaction-lock helper or tests/test_registry_single_writer_lock_818.py exists yet.
- Current registry source is paulsha_cortex/coordinator/registry.py. _reload_if_changed() short-circuits on equal mtime/size; _persist() writes the whole payload with _write_payload_atomically(); v1 migration directly backs up/replaces state; v2 slice normalization calls _persist() from _load().
- manager_daemon.run_loop currently catches executor exceptions, persists a done error envelope, then removes the request file. Test that conflict follows this path and preserves diagnostic text before changing daemon code.
- Relevant regressions already present: tests/test_workflow_registry.py::test_v2_atomic_write_failure_rolls_back_memory_and_file and tests/test_coordinator_registry_headless.py::test_legacy_overwritten_verification_hash_is_migrated.
- README.md describes shared coordinator root near its coordinator-root configuration section; docs/unified-work-lifecycle.md describes jobs.json as lifecycle truth. Update those user-facing contracts for CAS/conflict behavior.
- The checkout has unrelated untracked planning documents under docs/superpowers/plans/. Preserve them. Implement from an isolated issue worktree on a policy-compliant feature branch; do not use or clean the shared main checkout.

## Five-dimension sizing

Sizing is for this child, not full #818:

| Dimension | Score | Basis |
|---|---:|---|
| domain_breadth | 0 | Expected production implementation is one module: coordinator/registry.py. If tests prove manager_daemon.py also needs production changes, revise this dimension and recompute before expanding scope. |
| state_consistency | 2 | Exact durable revision, cross-process compare-and-persist, conflict recovery and process-death lock release. |
| acceptance_surfaces | 2 | the current sizing adapter feeds the full process-level set R-09/R-16/R-19, so signal = 2 gate_spine + 3 rules = 5, which scores 2. This follows current_sizing_snapshot() even though this child adds no CLI command. |
| spec_stability | 0 | Complete accepted spec/design/plan with no blocking markers, using stability-risk-v2. |
| orchestration | 2 | fix-standard has 9 cards; all 9 referenced cards have persona_binding. |

Expected total: 6 / Yellow. This is a projection from the current fix-standard combo and current policy surfaces, not an implementation result. Before implementation, use the then-linked combo and actual applicability inputs with compute_sizing_score(). Recompute if combo, contract rules, affected production modules, or artifact completeness changes. If the result is Red, keep #966 acceptance intact and report an issue-backed split; do not downscope silently.

## Tasks

- [x] **T1 tests / RED — real same-revision writers (R9; AC 1–3, 8)**: Add tests/test_registry_single_writer_lock_818.py. Use subprocess.Popen and a bounded file/event barrier so two independent JobRegistry instances each signal that they loaded revision N before either mutation proceeds. Cover different-slice and same-slice record_action races. For each run collect child exit/result data and inspect raw jobs.json. Different-slice outcome must be either both durable or one durable plus an explicit RegistryRevisionConflict; no silent lost update. At least one deterministic case must reach the conflict branch. Do not replace the process race with a single-process mock.
- [ ] **T2 source / identity and exact revisions (R2–R4; AC 5–6)**: In coordinator/registry.py add canonical_state_path and state_transaction_lock_path, exact raw-byte revision tracking with a distinct absent sentinel, and a durable-read helper that treats only true absence as absent. Make _reload_if_changed() detect different bytes even when size and restored mtime match. Add a blocking O_RDWR|O_CREAT, no-O_TRUNC exclusive flock sidecar. Keep it after unlock and do not write owner data. Test canonical path stability for symlink-directory spellings and state-file symlink replacement semantics.
- [ ] **T3 source / CAS and conflict restore (R1, R5; AC 1–3, 5)**: Route _persist() through transaction-lock compare-and-persist. Only invoke the existing atomic writer if actual durable revision equals the registry’s loaded revision. Add RegistryRevisionConflict with expected_revision, actual_revision and canonical state_path. On mismatch, do not write, merge, retry or replay; fully reload the durable snapshot into registry memory and sequence while suppressing nested repair writes. Missing state returns to the constructor seq_start baseline. Keep durable parse errors fail-closed.
- [ ] **T4 source / close every JobRegistry write path (R6, R8; AC 7)**: Move v1 migration’s backup-plus-replace under the same lock and expected-byte comparison; check revision before creating the backup so a stale migration has no side effects. Keep the original backup payload and atomic migration semantics. Keep v2 slice normalization on the same CAS path. Make rollback/conflict reload safe from recursively taking the transaction lock. Search every state-path writer after implementation and prove none bypass the guard.
- [ ] **T5 tests / Manager request acknowledgement (R7; AC 4)**: Through the real manager_daemon request drain path, inject a competing JobRegistry commit after the request registry loaded revision N, then force the request mutation to persist. Assert that action’s done file is durable with status=error and diagnostic text includes conflict type, expected/actual revision and canonical path; assert no status=ok. Request removal is allowed only after that explicit error done is durable and must not mean the mutation succeeded. If the current generic exception boundary fails these assertions, make only the necessary Manager change, then update domain_breadth and recompute sizing.
- [ ] **T6 compatibility, documentation and changelog (R8; policy surfaces R-09/R-16/R-19; AC 7)**: Preserve JSON roots/schema_version, public method return values, single-writer semantics, atomic fsync/rollback and v1 backup behavior. Keep the existing rollback and migration regressions green. Update README.md’s shared coordinator-root contract and docs/unified-work-lifecycle.md’s durable registry description to explain stale-revision rejection and explicit request error. Add the eventual feature’s changelog.d fragment and CHANGELOG.md [Unreleased] entry per repo policy. Do not add a CLI.
- [ ] **T7 focused and required verification (all AC)**: Run the new process-race module; tests/test_workflow_registry.py; tests/test_coordinator_registry_headless.py; relevant Manager request tests; and full pytest. Run policy_check with actual PR context so R-09/R-19 are evaluated. Record exact commands and final lines, including skipped or environment-limited gates. Do not claim runtime multi-Manager or doctor inventory evidence here; those belong to #967 and parent #818.

## Acceptance traceability

| #966 acceptance | Required evidence |
|---|---|
| Two JobRegistry processes loaded revision N; different slices; no stale overwrite | T1 process barrier, both raw-state result and per-process outcomes |
| Same-slice deterministic conflict, not success | T1 deterministic branch assertion; T5 error acknowledgement if via request |
| Conflict restores durable memory and reports expected/actual/path | T3 field assertions plus all collections/seq snapshot comparison |
| Manager request conflict becomes durable done error, never ok | T5 real request drain integration |
| Same-size bytes plus restored mtime is detected; stale persist conflicts | T2 external replacement and stale-instance commit tests |
| SIGKILL lock holder releases lock; fresh registry persists; sidecar/no owner | T2/T7 real subprocess crash-recovery test |
| Migration/normalization included; compatibility and rollback retained | T4 and T6 migration, normalization, schema/API/rollback regressions |
| Real process barrier in the named test module | T1 test file location and subprocess assertion |

## Stop conditions and non-goals

- If two processes both loaded revision N, no implementation may let the later full-snapshot write silently erase the first mutation. A conflict is an acceptable result; automatic field merge, retry or transition replay is not in scope.
- If conflict recovery cannot reload a valid current durable snapshot without recursively writing, stop implementation at a focused failing test and revise the design before claiming completion.
- No Manager lifetime owner lock, second-Manager startup policy, owner record/diagnostics or doctor /proc inventory (#967).
- No installer/path default changes, per-instance coordinator-root migration, state payload/schema bump, generic external filesystem writer coordination, NFS/hard-link guarantee, #821 persistence hygiene, #153 external-delete synchronization, or #497 terminal replay change.
- No edits to GitHub, Cortex state, the shared checkout’s unrelated untracked documents, or this accepted plan text other than marking completed task boxes after implementation.

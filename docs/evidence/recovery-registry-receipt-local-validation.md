# Recovery registry receipt — local validation record

This record covers the local `#862` recovery-registry-receipt candidate on
`feature/862-recovery-registry-receipt`. It documents pre-archive validation
only. It does not claim archive, merge, issue closure, installation, loaded
runtime verification, or any downstream B/C/D recovery action.

## Local gates

| Check | Command shape | Exit | Observation |
| --- | --- | ---: | --- |
| Focused pytest | `env -u PSC_REPO_ROOT $VENV_PYTHON -m pytest tests/test_recovery_registry_receipt_497.py tests/test_planning_completeness.py tests/test_wiring_claim_time_sizing.py tests/test_wiring_yellow_plan_review.py tests/test_workflow_registry.py tests/test_coordinator_registry_headless.py tests/test_registry_released_claimkey_load.py tests/test_registry_decomposition_depth_223.py tests/test_registry_sizing_band.py -q` | 0 | `137 passed, 3 subtests passed` |
| Pytest gate scope | `env -u PSC_REPO_ROOT $VENV_PYTHON -m pytest -q` | 0 | `6234 passed, 44 skipped, 186 subtests passed` |

## CLI help smoke

Both commands ran from a temporary directory outside the checkout with
`PYTHONPATH=$CANDIDATE_ROOT` and without calling a live manager.

| Check | Command shape | Exit | Observation |
| --- | --- | ---: | --- |
| Top-level help | `python -m paulsha_cortex.cli --help` | 0 | rendered non-empty help text (`2718` bytes) |
| `run work` help | `python -m paulsha_cortex.cli run work --help` | 0 | rendered non-empty help text (`3176` bytes) |

## Planning-boundary observations

- `docs/superpowers/workstreams/recovery-registry-receipt/todo.md` and
  `openspec/changes/archive/2026-09-22-recovery-registry-receipt/tasks.md`
  still carry identical `domain_breadth` / `state_consistency` /
  `invariant_count` / `artifact_classes`, and their checklist lines remain
  byte-identical after normalizing `[x]` to `[ ]`.
- The accepted intake authority tracked in this repository remains present
  under the manager-owned archive:
  `openspec/changes/archive/2026-09-22-recovery-registry-receipt/proposal.md`,
  `openspec/changes/archive/2026-09-22-recovery-registry-receipt/design.md`,
  `openspec/changes/archive/2026-09-22-recovery-registry-receipt/tasks.md`, and
  `openspec/changes/archive/2026-09-22-recovery-registry-receipt/specs/recovery-registry-receipt/spec.md`.
  This card only validated the presence and internal agreement of those files;
  it does not claim archive, merge, or any extra publication authority beyond
  the current repository revision.
- The accepted Superpowers trio, accepted OpenSpec trio, and combined six-file
  bundle each remain complete under `assess_planning_completeness()`.
- `work_bridge.current_sizing_snapshot()` still projects those tracked accepted
  bundles to `(6, "yellow")`; this is documented as a projection only and does
  not by itself authorize dispatch, archive, or `done`.
- `docs/unified-work-lifecycle.md` now records that
  `prepare_recovery()` / `commit_pre_candidate_recovery()` /
  `checkpoint_legacy_binding()` remain registry-only primitives; they do not
  add new public CLI verbs, do not grant allowed-action authority, and do not
  reconcile queue/done or CompletionRecord on behalf of B/C/D.

## Post-archive intake evidence

The frozen wording that says publication, mapping, and binding were still pending
records the state when the accepted plan was written. The committed intake landed
on 2026-09-08 and is an ancestor of the frozen build base merged on 2026-09-21
(at 20:31 +08). It contains the accepted child OpenSpec proposal, design, spec,
tasks, and the unique `#862` work-item mapping. The durable workflow record was
created later on 2026-09-21 (20:54 +08), binds issue `#862` and OpenSpec
`recovery-registry-receipt`, records a passed plan review, and pins the plan,
accepted spec/design, and todo inputs. The stored plan digest matches the
unchanged frozen plan snapshot.

The todo and archived OpenSpec task prose remain unchanged; their historical
wording is time-scoped to the pre-publication baseline. The existing T10 checkbox
in the todo is checked, and the archived task checkbox is now checked to reflect
the verified prerequisites and this card's changelog/local-validation alignment.
The frozen plan input was not modified. The current worktree retains the official
archive and does not recreate an active OpenSpec change.

## Redispatch regression check

After merging the fetched `origin/main` into the Candidate tree, the five
operator-required regression files passed: `102 passed`. The check covered
recovery registry receipts, workflow registry, planning completeness, claim-time
sizing, and yellow-plan review.

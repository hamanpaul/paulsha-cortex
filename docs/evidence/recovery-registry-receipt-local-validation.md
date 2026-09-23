# Recovery registry receipt — local validation record

This record covers the local `#862` recovery-registry-receipt candidate on
`feature/862-recovery-registry-receipt`. It documents pre-archive validation
only. It does not claim archive, merge, issue closure, installation, loaded
runtime verification, or any downstream B/C/D recovery action.

## Local gates

| Check | Command shape | Exit | Observation |
| --- | --- | ---: | --- |
| Full pytest preflight | `env -u PSC_REPO_ROOT $VENV_PYTHON -m pytest -q` | 0 | `6367 passed, 44 skipped, 186 subtests passed` on the merged current-main tree |

## CLI help smoke

Both commands ran from a temporary directory outside the checkout with
`PYTHONPATH=$CANDIDATE_ROOT` and without calling a live manager.

| Check | Command shape | Exit | Observation |
| --- | --- | ---: | --- |
| Top-level help | `python -m paulsha_cortex.cli --help` | 0 | rendered non-empty help text (`2718` bytes) |
| `run work` help | `python -m paulsha_cortex.cli run work --help` | 0 | rendered non-empty help text (`3176` bytes) |

## Planning-boundary observations

- `docs/superpowers/workstreams/recovery-registry-receipt/todo.md` and
  `openspec/changes/recovery-registry-receipt/tasks.md`
  still carry identical `domain_breadth` / `state_consistency` /
  `invariant_count` / `artifact_classes`, and their checklist lines remain
  byte-identical after normalizing `[x]` to `[ ]`.
- The accepted intake authority is present in the active change at
  `openspec/changes/recovery-registry-receipt/`. Proposal, design, task metadata,
  and spec content remain the accepted baseline; this candidate changes only
  existing task checkboxes. The active task list and workstream todo have the
  same 14 pre-archive tasks after checkbox normalization.
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

## Verified intake timeline and candidate state

The frozen wording that says publication, mapping, and binding were still pending
records the state when the accepted plan was written. The committed intake landed
on 2026-09-08 and is an ancestor of the frozen build base merged on 2026-09-21
(at 20:31 +08). It contains the accepted child OpenSpec proposal, design, spec,
tasks, and the unique `#862` work-item mapping. The durable workflow record was
created later on 2026-09-21 (20:54 +08), binds issue `#862` and OpenSpec
`recovery-registry-receipt`, records a passed plan review, and pins the plan,
accepted spec/design, and todo inputs. The stored plan digest matches the
unchanged frozen plan snapshot.

The todo and active OpenSpec task prose remain unchanged; their historical
wording is time-scoped to the pre-publication baseline. T10 is checked in both
task lists to reflect the verified prerequisites and this card's changelog/local-
validation alignment. The frozen plan input was not modified. Earlier archive
commits remain in Git history, while this candidate restores the active change
so the task list represents pre-archive work. The Manager-owned archive action
remains pending and is not claimed here.

## Redispatch regression check

The current-main merge includes the service-PATH fixture fix from #984 and
preserves both the #862 and mainline Unreleased entries. The full pytest
preflight passed on this merged tree, including the previously failing
service-PATH fixture.

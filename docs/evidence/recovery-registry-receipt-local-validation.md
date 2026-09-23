# Recovery registry receipt — local validation record

This record covers local validation for the `#862` recovery-registry-receipt
work on `feature/862-recovery-registry-receipt`. The gate result below belongs to
the earlier merged-main candidate, before this archive-state repair. The existing
official archive and promoted spec are preserved by this repair. This card does
not claim a new archive action, merge, issue closure, installation, loaded
runtime verification, or any downstream B/C/D recovery action.

## Earlier candidate gate

| Check | Command shape | Exit | Observation |
| --- | --- | ---: | --- |
| Full pytest | `env -u PSC_REPO_ROOT $VENV_PYTHON -m pytest -q` | 0 | `6367 passed, 44 skipped, 186 subtests passed` on the earlier merged current-main tree, before this archive-state repair |

## CLI help smoke

Both commands ran from a temporary directory outside the checkout with
`PYTHONPATH=$CANDIDATE_ROOT` and without calling a live manager.

| Check | Command shape | Exit | Observation |
| --- | --- | ---: | --- |
| Top-level help | `python -m paulsha_cortex.cli --help` | 0 | rendered non-empty help text (`2718` bytes) |
| `run work` help | `python -m paulsha_cortex.cli run work --help` | 0 | rendered non-empty help text (`3176` bytes) |

## Planning-boundary observations

- `docs/superpowers/workstreams/recovery-registry-receipt/todo.md` and the
  archived `openspec/changes/archive/2026-09-22-recovery-registry-receipt/tasks.md`
  still carry identical `domain_breadth` / `state_consistency` /
  `invariant_count` / `artifact_classes`, and their checklist lines remain
  byte-identical after normalizing `[x]` to `[ ]`.
- The accepted intake authority remains in the official archive at
  `openspec/changes/archive/2026-09-22-recovery-registry-receipt/`, with the
  promoted spec at `openspec/specs/recovery-registry-receipt/spec.md`. The
  active change path is absent. The archived task list and workstream todo have
  the same 14 pre-archive tasks after checkbox normalization.
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

The todo and archived OpenSpec task prose remain unchanged; their historical
wording is time-scoped to the pre-publication baseline. T10 is checked in both
task lists to reflect the verified prerequisites and the changelog/local-
validation alignment. The frozen plan input was not modified. The previous
Candidate already contained the official archive and promoted spec; this repair
restores that state after the latest-main merge recreated an empty active change
directory. The active path is absent, and this card did not run a new archive
action.

## Redispatch regression check

The current-main merge includes the service-PATH fixture fix from #984 and
preserves both the #862 and mainline Unreleased entries. The earlier full pytest
run covered that merged tree, including the previously failing service-PATH
fixture; the current repair candidate requires its own authoritative preflight
and pytest gate.

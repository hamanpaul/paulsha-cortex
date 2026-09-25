## Tasks

- [ ] Add the shared typed `_persist_main_sync_stop` writer in `manager.py`; persist needs_human facet/reason and `context.main_sync` together while retaining delivery reason/detail and sibling context.
- [ ] Connect the passed-review `advance-ship` replay stop in `apply_workflow_action`.
- [ ] Connect the final-review-evidence `advance-phase` transition stop.
- [ ] Add wrapper-level tests for both branches and read the same WorkflowRun back from registry storage after each wrapper returns.
- [ ] Assert exact round-trip of valid C or the original invalid/abbreviated pre-validation C observation, M/null, every full conflict path, repair/skipped/failure members, original delivery reason/detail, and sibling context; invalid-C read-back keeps M=null and no retry-build action.
- [ ] Add synthetic `repair-budget-exhausted` and `registry-reset-refused` writer/read-back cases without invoking repair, reset, or Builder.
- [ ] Project status hints from the #989 recovery helper result; test missing/corrupt context and write/read-back failures suppress unavailable retry-build hints.
- [ ] Update lifecycle/status documentation and run the required CLI help parity check; stop for issue-backed scope review if a CLI change is needed.
- [ ] Add an implementation changelog fragment and `[Unreleased]` entry; run targeted/repository tests, OpenSpec validation, PR-context policy, and `git diff --check` before delivery.

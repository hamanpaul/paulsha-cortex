---
status: accepted
work_item: manager-main-sync-context
issue: 990
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
domain_breadth: 0
state_consistency: 2
acceptance_surfaces: 2
spec_stability: 0
orchestration: 2
total_score: 6
sizing: yellow
---

# Manager main-sync stop context todo

## Tasks

- [ ] **Source**: Add `_persist_main_sync_stop(run_id, context, ...)` in `manager.py` for the #988 typed `MainSyncContext`; persist needs_human facet/reason and `context.main_sync` in one WorkflowRun update while retaining existing delivery reason/detail and sibling context fields.
- [ ] **Source**: Connect the actual `apply_workflow_action` passed-review `advance-ship` replay stop branch to the shared writer.
- [ ] **Source**: Connect the actual final-review-evidence `advance-phase` transition stop branch to the shared writer.
- [ ] **Tests**: Exercise each Manager wrapper branch independently; read the same WorkflowRun back from the registry store after the wrapper returns and exact-compare valid C or the original invalid/abbreviated pre-validation C observation, M/null, every full path, repair/skipped/failure fields, original delivery reason/detail, and existing sibling context fields. The pre-validation failure must preserve M=null and leave retry-build unavailable.
- [ ] **Tests**: Prove fetch-after-valid-M failures read back with the same exact M and early failures read back with M and `failure.main_head` both null; retain all nested types and long paths through durable serialization.
- [ ] **Tests**: Use typed synthetic `repair-budget-exhausted` and `registry-reset-refused` contexts through the shared writer and durable read-back; do not run repair, reset, or Builder dispatch.
- [ ] **Tests**: Verify `workflow_status_entry` hints follow #989 action availability/result, unavailable retry-build is not shown as available, and the two permitted resume cases match the helper result.
- [ ] **Tests**: Inject durable update failure and read-back mismatch; assert no retry-build hint is exposed and unrelated needs_human context remains intact.
- [ ] **Documentation / CLI**: Update the appropriate lifecycle/status documentation for the operator-visible hints; confirm `cortex --help` has no new command or option, and stop for issue-backed scope review if CLI changes prove necessary.
- [ ] **Changelog / gates**: Add an implementation changelog fragment and `[Unreleased]` entry; run targeted Manager/status regressions, the required test suite, OpenSpec validation, PR-context policy, and `git diff --check` for the implementation PR.

## Sizing inputs

The live #990 acceptance criteria define eight invariants. The planned production scope is one module (`manager.py`), so domain breadth=0. The same WorkflowRun must persist facet/reason/typed context and then survive registry read-back, so state consistency=2. The current `fix-standard` combo supplies 2 gate-spine entries; adding R-09, R-16, and R-19 gives acceptance surfaces=2. This accepted spec/design/todo set has no unresolved planning blocker, so spec stability=0. The combo has 9 cards and 9 persona bindings, so orchestration=2. Repository `current_sizing_snapshot()` is the authority; the recorded total is 6 / Yellow only if its live result agrees.

## Dependencies and boundary

Hard product prerequisites are #987 probe producer implementation/acceptance, #988 an updated typed contract that carries invalid/abbreviated pre-validation C as non-authorizing diagnostic evidence with M=null plus its implementation/acceptance, and #989 recovery action helper implementation/acceptance. Live dependency snapshot: #987 remains OPEN after its first implementation job was dispatched; #988 has Draft planning PR #1056 at head `8e7a56a9` with no implementation acceptance; #989 has Draft planning PR #1061 at head `ac2ebae5`, no implementation acceptance, one policy check failed, and its pytest matrix was still running at the last query. This planning PR does not satisfy any prerequisite and does not authorize Cortex intake or product implementation.

This child corresponds to #972's durable same-run C/M context, wrapper preservation, store read-back, and status hint consistency. It does not reduce #972 or #943 acceptance. Automatic repair/reset, Builder, candidate generation, merge commit, push, and the #973 full integration remain outside this work item.

---
status: accepted
work_item: manager-main-sync-context
issue: 990
---

## Context

The live #990 contract requires Manager to persist the updated typed #988 `MainSyncContext` into the same WorkflowRun whose facet/reason becomes `needs_human`. The updated contract must represent pre-validation failures with the original invalid/abbreviated Candidate observation as diagnostic-only and M=null. The writer must preserve prior delivery reason/detail, keep sibling context, and expose status hints consistent with #989's action helper. The persisted store read-back is the acceptance proof; an evidence file or in-memory object is insufficient. A pre-validation stop must read back its original observation, M=null, and no retry-build availability.

## Decisions

1. **One durable writer**: `_persist_main_sync_stop(run_id, context, ...)` owns the facet/reason/context update. It consumes the updated typed value without stringification or field loss, preserves invalid/abbreviated pre-validation Candidate observations as diagnostic-only, and preserves existing sibling context.
2. **Two real call sites**: Connect the passed-review `advance-ship` replay in `apply_workflow_action` and the final-review-evidence `advance-phase` transition separately. Both tests pass through the actual Manager wrapper.
3. **Store read-back**: After each wrapper returns, reopen/read the same `run_id` from registry storage and exact-compare valid C or the original invalid/abbreviated pre-validation observation, M/null, paths, repair/skipped/failure members, original delivery reason/detail, and existing context. A pre-validation failure keeps M=null and retry-build unavailable.
4. **Synthetic future stops**: Feed `repair-budget-exhausted` and `registry-reset-refused` typed payloads through the same writer and store read-back. These cases do not invoke repair, reset, or Builder.
5. **Status is derived from #989**: `workflow_status_entry` uses the recovery helper's action availability/result. Missing or malformed context, invalid/abbreviated C, M=null, writer failure, or read-back mismatch cannot produce an available `retry-build` hint.

## Sizing

The planned production scope is one module (`manager.py`): domain breadth 0. Same-run facet/reason/context persistence and durable read-back require state consistency 2. The `fix-standard` combo's 2 gate-spine entries plus R-09/R-16/R-19 yield acceptance surfaces 2. Complete accepted planning gives spec stability 0; 9 cards with persona bindings yield orchestration 2. Recompute with repository `current_sizing_snapshot()` before intake; the expected total is 6 / Yellow.

## Dependencies and rollout

Do not begin product implementation until #987 and #989 are accepted and #988 has updated and accepted its typed contract for invalid/abbreviated pre-validation Candidate failures, followed by implementation and acceptance evidence for all three prerequisites. Publish the planning material and unique binding first. The eventual product PR must revalidate exact issue text and dependency heads, rerun targeted and repository gates, and preserve #972/#943 acceptance. #990 does not run automatic repair/reset, dispatch Builder, merge, or push.

---
status: accepted
work_item: manager-main-sync-context
issue: 990
---

# Manager main-sync stop context 提案

## Why

Live #972 requires the typed probe failure to remain durable on the same WorkflowRun after Manager wraps the stop, including failures that occur before Candidate validation. The updated #988 contract must preserve the original invalid/abbreviated Candidate observation as diagnostic-only and leave M null in that case. The two ship-validator wrapper branches must not discard the nested value, and operator status must not claim a recovery action that #989 does not expose. Without a Manager writer plus store read-back, delivery evidence alone cannot authorize or explain a later action.

## What Changes

Define one Manager-owned writer contract for needs_human facet/reason plus `context.main_sync`, connect both specified ship-validator stop branches, verify durable read-back, and project next-step hints from #989's recovery helper result. Use synthetic typed contexts to prove future repair-skipped stops can be recorded without running repair.

## Impact

- Production scope: `paulsha_cortex/coordinator/manager.py` only.
- Depends on #987 probe producer, #988 typed `MainSyncContext`, and #989 recovery helper implementation and acceptance.
- No probe algorithm, recovery action, job selector, automatic repair/reset, Builder dispatch, merge commit, push, or #943 completion is included.
- #990 remains a planning-only child of #972; this proposal does not authorize intake or implementation.

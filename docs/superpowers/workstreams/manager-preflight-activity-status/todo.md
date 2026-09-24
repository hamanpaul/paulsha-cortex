---
status: draft
work_item: manager-preflight-activity-status
issue: 1028
domain_breadth: 1
state_consistency: 1
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
---

# Manager 長時間 PR preflight status Todo（#1028）

## Boundary and release decision

- Authority: live [#1028](https://github.com/hamanpaul/paulsha-cortex/issues/1028), including its three 2026-09-24 comments; trigger [#885 / PR #1024](https://github.com/hamanpaul/paulsha-cortex/pull/1024).
- **本輪非 release blocker。** 事件造成 status `degraded_reason=stalled` 的誤報；issue 記錄 preflight 完成後同一 Manager PID 恢復 status，`consecutive_tick_failures=0`。最新留言另記錄 #966/#965 已寫出 exit/log/gate 檔但 `cortex jobs` 到 07:09 UTC 仍為 `dispatched`。本票補可觀測性，不改 policy/pytest/review/card gate/merge 判準或成功證據。Latest release `v0.1.10` 已於 2026-08-27 發布；#1028 為 OPEN、無 milestone/label。
- **#885/#1024 gate 狀態與 owner 保持原樣。** 截至本規劃核對，PR #1024 OPEN/unmerged，head `dac3c4b8776dbae5eff0aa0002378f725e07967a`、check rollup 空、兩條未解 Copilot thread，GitHub mergeability 為 `UNKNOWN`。#885 最新 09:26 UTC 留言記錄正式 run 已受理同一 Candidate 的 `retry-build` 並派出 builder 修兩項 finding 與 main 衝突；新 Candidate、verify、review、ship、PR merge 與 issue close 尚未完成。#1028 的 busy 或 process-exit observation 不能通過、豁免或推進 #885/#1024 gates。
- 排程：規劃可先完成；產品實作等 PR #1024 的 `work_bridge.py` ship/preflight 路徑穩定後再進，以其最終路徑作整合基線。#1028 不需阻塞 #885/#1024 自身 review/check 驗收。
- No dependency on #781 or #488. No preflight authorization or release promotion follows from busy or process exit.
- Candidate aggregate `work_id`: `manager-preflight-activity-status`; producer/consumer IDs remain candidates. 本 PR 保持 `status: draft`，不登記 WorkAuthority、不建立 Cortex run；實際登記前須作 canonical uniqueness/authority binding 檢查並 read back。

## Five-dimension sizing

Issue `fix(control)` maps through packaged `task-types.yaml` to `fix-standard`: 9 cards, 9 persona bindings, 2 core gate spine entries. Current process rules are R-09/R-16/R-19, so acceptance signal is 2+3=5 and `acceptance_surfaces=2`.

- `domain_breadth=1`: one narrow Manager/control status flow across producer and projection; no change to gate, authority, review or ship behavior.
- `state_consistency=1`: a short-lived activity lease is atomically written/read/cleared with owner PID and operation ID; no registry/gate state mutation.
- `acceptance_surfaces=2`; `orchestration=2` from fix-standard metadata.
- Re-ran the official `current_sizing_snapshot()` helper on these reconciled draft artifact rows at base `8b26d3702c398cb8dbda337bd596e26a0d96bc5b`: **(8, `red`)** (`domain_breadth/state_consistency/acceptance_surfaces/spec_stability/orchestration = 1/1/2/2/2`). Draft status contributes `spec_stability=2`; this is not registration or acceptance. The intake report also had a status-only accepted projection at **(6, `yellow`)** for the earlier scope; that was a counterfactual, not root acceptance.
- The 07:10 UTC issue comment adds a pending job-exit observation requirement absent from the intake projection. The current draft score is remeasured, but the earlier accepted projection is stale and cannot establish acceptance. Re-run the official helper on the accepted artifacts at formal intake; if that accepted set still exceeds Yellow, stop for issue-backed decomposition rather than dropping the observation boundary.
- Producer and consumer packages may proceed in parallel only after the v1 activity and exit-receipt contracts are frozen; the consumer depends on the exact contracts, not completed producer code.

## Invariants / acceptance

1. Only the Manager's exact ship PR-preflight path creates an activity lease; callers cannot inject work identity, busy state or child PID.
2. The lease binds one operation ID, owner Manager PID, repo/work/run, candidate, stage and child process; exact child must remain in that Manager's process tree.
3. `last_progress_at` advances only on observed process output or a verified stage transition, never on a periodic timer alone.
4. Valid live child plus recent progress beyond the ordinary 120-second status age returns visible busy with work identity; it does not claim ready or passed.
5. Manager death, lost child, bad parent/PID/schema, stale progress or failed process probe remains degraded; a stale lease cannot mask a later operation or stay busy forever.
6. Busy projection sets `daemon.idle=false`, so the existing capacity gate still asks before expensive spawn.
7. Old status lacking activity data follows current behavior; preflight command/result, review/merge authorization, gates, evidence and failure counters remain unchanged.
8. A bound job exit receipt remains a read-only `process_exit_observed` / `manager_acceptance=pending` observation until the existing Manager owner accepts it; status never terminalizes, classifies a gate or dispatches follow-up work.

## Tasks

- [ ] **T0 freeze baseline/sequence**: Recheck #1028 and #1024 state at implementation intake; land/rebase after #1024's `work_bridge.py` hunk is stable. Preserve the latest main status behavior and existing live-busy/stalled tests.
- [ ] **T1 activity/receipt contract freeze**: Define exact `cortex-manager-activity/v1` fields, PID/operation ownership, bounded path, timestamp format, stage enum, size cap and atomic replace/owner-checked clear. Also define the read-only job observation tuple: exact job ID + canonical registry row + that row's bound exit-receipt path, emitted fields, and `manager_acceptance=pending` semantics. Unknown/unbound receipt means no exit claim. Freeze both contracts before consumer code starts; expose no secret, absolute local path, raw command line or gate payload.
- [ ] **T2 producer integration (`manager-preflight-activity-producer`)**: At the exact Manager PR preflight call, publish run/repo/candidate identity before launching; record current policy/CI child and observed progress; clear only same owner/operation in all return/exception paths. Keep existing subprocess argv/environment/capture/exit contract. Add fault behavior for activity write failure: preserve preflight result but status must not gain busy exemption.
- [ ] **T3 consumer and CLI (`manager-busy-status-projection`)**: Read/validate activity; require live matching Manager PID, live child ancestry and progress within `STATUS_STALLED_AFTER_SECONDS`. Valid busy returns `degraded=false`, explicit activity fields and `daemon.idle=false`; all unknowns retain fail-closed status. Read-only probe exact registered exit receipts and, when the registry remains `dispatched`/`running`, show process exit separately from pending Manager acceptance. Print activity and pending job identity in text and JSON; do not update rows, accept gates or dispatch.
- [ ] **T4 negative/compat tests**: Long (>120 sec) fake preflight with observed output; normal completion; nonzero policy/pytest result; Manager dead; child exit; unrelated/orphan child; mismatched owner/run/PR/candidate; missing/unknown/corrupt/stale lease; old status schema. Add exact job receipt present while registry stays dispatched (show exit observed + acceptance pending), mismatched/unbound receipt (no exit claim), and prove status read does not terminalize, classify a gate, or dispatch. Assert no gate-pass implication or command/env secret disclosure.
- [ ] **T5 capacity and regression**: Assert `capacity_gate.evaluate_gate()` returns ask for activity-derived busy; keep `tests/test_control_client.py` live busy/dead/stalled tests; add inspector text/JSON assertions. Do not change `capacity_gate` behavior or weaken 120-second unknown-state fail closed.
- [ ] **T6 docs/changelog and required gates**: Document busy vs degraded status and the 120-second proof conditions; during implementation add policy-required changelog fragment and CHANGELOG entry; run focused tests, repository-required checks, and PR-context `policy_check` on the exact head. No CLI action or release gate changes.
- [ ] **T7 completion accounting**: Report source/tests/merge and any installed/live status evidence separately. A code/test pass does not prove an installed Manager uses the activity lease; no service restart or live canary is part of this planning task.

## Completion boundary

Complete #1028 only after an exact long-preflight production-path fixture proves the busy lease and status projection end to end; a bound job receipt proves exit-observed/pending-acceptance without mutating registry or gate state; all negative cases fail closed; the capacity gate remains ask; and existing preflight outcomes are unchanged. Recheck #885/#1024 and the runtime delivery gates at implementation intake. Do not close #885/#1024, claim their gates passed, or claim release readiness from this status fix.

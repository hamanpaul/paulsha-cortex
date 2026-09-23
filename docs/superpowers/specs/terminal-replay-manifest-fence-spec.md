---
status: accepted
work_item: terminal-replay-manifest-fence
---

# `complete_tick` 權威 attempt 判定與 handoff manifest 覆寫 fence 規格

## Requirements

對應 [#481](https://github.com/hamanpaul/paulsha-cortex/issues/481)：同一 task（`slice_id`）有多個 terminal job 時，`complete_tick` 每 tick 依建立順序逐一重處理，唯一冪等判斷是單槽 handoff manifest 的 `job_id`，於是 A→B→C 輪流整份覆寫 manifest（每次重寫 `completed_at`、抹掉 `superseded_*` 稽核欄位）；slice lane 的舊 job 還會寫 `missing-slice-proof` evidence，並把 operator `recover-pre-candidate` 撥回的 `pending` 改回 `failed`。前置 [#862](https://github.com/hamanpaul/paulsha-cortex/issues/862)（work item `recovery-registry-receipt`，PR #954）提供 job 的 `supersession`／`consumption` disposition 欄位；本票只讀、不寫。

1. **R1 權威 attempt admission**：`complete_tick` 對每個 terminal job，在既有 `_is_safe_slice_id` 與 manifest symlink 檢查之後、`_existing_manifest_job_id` 短路以及任何 `_repo_root_for_slice_row`／`_candidate_for_evidence`／verification runner／evidence 寫入／`handoff.write_manifest`／`registry.update_slice`／`registry.record_action`／`record_executor_backoff_from_job`／foreign review launch 之前，判定它是不是該 task 的權威 attempt。非權威者為 audit-only：上述副作用一律不做（包含不對它的 branch／worktree 跑 `git rev-parse`），不進 `completed`／`completed_jobs`／`errors`／`warnings`；job row 與既有 evidence 原樣保留。判定依序如下，任一命中即 audit-only：
   - (a) **disposition**：job 的 `supersession` 或 `consumption` 為 mapping（#862 additive 欄位）。legacy row 缺這兩欄時視為沒有 disposition，不推定 consumed。
   - (b) **slice lane 綁定**：registry 有該 slice row 時，`kind == "review"` 的 job 需 `reviewer_job_id == job_id`，其餘需 `builder_job_id == job_id`；不符即為非現行 attempt，**不得**再落入 `slice_row is None` 的 `missing-slice-proof` 分支。builder 已有 `reviewer_job_id` 時既有的 `continue` 保留。
   - (c) **operator fence**：同一 slice row 的 `actions` 內有 `action ∈ {"operator-recover-pre-candidate", "operator-abandon"}` 且該 entry 的 `at` 不早於 job 的 `created_at`（以 `datetime.fromisoformat` 解析比較；任一方缺失或不可解析視為命中）→ audit-only。即使 `builder_job_id` 仍指向該 job 也一樣：main 上 recover 呼叫的 `update_slice(builder_job_id=None)` 不會清欄位。
   - (d) **無 slice row（workflow lane 與真正缺 slice）**：本輪開頭取得的 `registry.list_jobs()` snapshot 中，若有同 task、位置在後（建立較晚）、且 snapshot 狀態已屬 `TERMINAL_STATUSES` 的 job → audit-only。只有最新的 terminal attempt 可以終局化。最新 attempt 維持既有語意：workflow lane 寫 `WORKFLOW_LANE_GATE_STATUS`，真正缺 slice 寫 `needs_human`／`missing-slice-proof`（fail-closed）。
   - (e) **manifest fence**：見 R2。
2. **R2 manifest 覆寫 fence**：既有 manifest 可解析，且 (i) `superseded_at` 非 null、`job_id` 等於本 job，或 (ii) `job_id` 是 snapshot 中比本 job 建立較晚的 job → 本 job audit-only（R1 (e)）。同一判定在 `handoff.write_manifest` 之前，用**重新讀取**的 manifest 再做一次（deferred write fence）。命中時不寫檔、不進 `completed`／`completed_jobs`，並在 `warnings` 追加 `{"slice_id": <slice_id>, "job_id": <job_id>, "warning": "manifest-write-fenced"}`。manifest 缺檔或壞檔、`job_id` 不在 snapshot 內（未知），都不構成 fence；建立較晚的 attempt 覆寫較舊（包含已 superseded）的 manifest 照常允許。
3. **R3 manifest 內容冪等**：權威 job 要寫的 payload 與既有 manifest 的 `job_id` 相同、既有 manifest 沒有 `superseded_at`，且兩者經 JSON 正規化後除 `completed_at` 外完全相同 → 不寫檔（bytes 與 mtime 不變，保留原 `completed_at`）。`completed`／`completed_jobs` 的回報與現行相同。
4. **R4 收斂**：同一 task 不論有多少個 terminal job，只要 registry 以外的輸入不變（`git_runner`／verification runner／review launcher 的回應、spec／plan 檔、環境設定都相同，只有 `clock` 不同），第一次 `complete_tick` 之後再跑任意次，manifest bytes、slice 的 `state`／`gate_state`／`builder_job_id`／`reviewer_job_id`／`candidate`／`current_evidence_refs`、`actions`／`evidence_history` 長度、`<coordinator_root>/evidence/verification/` 的檔案集合都不變，也不產生 `quarantine/`。判定全部由 registry 持久資料與 manifest 導出，fresh `JobRegistry`（模擬 daemon restart）之後同樣成立。輸入改變時（例如 transient 故障解除、candidate 已 merge），current attempt 依 R6 重處理，結果可以改變，這不違反 R4；例：`tests/test_coordinator_manager.py` 的 `test_manifest_hides_evidence_when_slice_update_fails` 第二輪改寫 manifest 與 state。
5. **R5 recovery 單調**：`recover-pre-candidate` 回 `pending` 之後，在它之前建立的所有 terminal job（包含仍被 `builder_job_id` 指向者）都不得把 slice 撥回 `failed`／`needs_human`、不得重綁 binding、不得改寫 `provider_outcome`；已 superseded 的 manifest 保留 `superseded_at`／`superseded_by`／`superseded_reason`，bytes 不變。manifest 缺檔時仍成立（fence 來自 registry）。`abandon` 同理。fence 之後建立並綁定的新 attempt 照常終局化，可以覆寫舊 manifest。
6. **R6 不倒退**：無 slice row 的同一 task，若兩個 job 都是本輪才 poll 成 terminal，依 (d) 的 snapshot 語意兩者依序終局化，`same-slice concurrent terminals` warning 恰記一次、`completed` 去重（`test_concurrent_same_slice_terminals_warn_and_dedup` 原斷言不改）。以下處理語意不變（R3 只省略內容相同的檔案寫入）：current attempt 的 verification／review／completion、`_existing_manifest_job_id` 回 None 時的重處理（passed／verified 的落地輪詢、`verification-runner-error` 等）、workflow lane gate、真正缺 slice 的 `missing-slice-proof`、#496 dirty recheck、#383 `dispatch_gate_scan`。
7. **R7 測試**：新增 `tests/test_terminal_replay_manifest_fence_481.py`，RED→GREEN 覆蓋：(a) slice lane A/B/C 跑兩次 tick：A exited、B failed，兩者都未綁定；C exited 且已綁定，verification 回 status `needs_human`（`VERIFICATION_RESULT_STATES` 不含 `failed`，`_apply_verification_result` 把它記為 `verification-failed` action）。修前第二輪會重跑 C 的 verification、多記一筆 `verification-failed`，A 則寫出 `missing-slice-proof` evidence。(b) 另建 fixture：三個 failed job，C 綁定、`candidate` 為 null；先跑兩次 tick，執行 recover 後再跑兩次 tick。(a) 的 C 驗證後 `candidate` 已是 SHA，而 recover 在這種情況下不被允許（`manager.py:649-669`、`:2032`），所以不能沿用 (a) 的 fixture。(c) 單一 failed job 搭配 None-set manifest（`needs_human`＋`verification_evidence_path: null`＋`pinned-input-mismatch`，slice 為 `needs_human`），recover 後 tick；另含 manifest 缺檔（job 在 recover 前從未終局化）與 fresh `JobRegistry` 兩個變體。選 `pinned-input-mismatch` 是因為 mismatch 分支（`:2537`）先於 failed 分支，是 failed job 能產生的 None-set 原因；`verification-runner-error` 只出現在 exited 的 verification 分支（`:2628`）。(d) workflow lane 同 task 三個 terminal job 跑兩次 tick；(e) deferred write fence，以及 manifest 指向較新 job；(f) 被 #862 `supersession`／`consumption` 標記、但仍綁定的 job；(g) recover 後的新 attempt 正常終局化並覆寫 superseded manifest；(h) verified slice 在 `candidate-not-merged` 輪詢下，第二輪 manifest bytes 不變。斷言涵蓋 action 數、evidence refs、manifest sha256、`builder_job_id`、state、gate_state；既有 tests 原斷言保留。

## Boundary

Production 只改 `paulsha_cortex/coordinator/manager.py`（`complete_tick` 與新增的 pure helper）；`registry.py`、`persona/handoff.py`、`verification.py` 不改。

與 [#497](https://github.com/hamanpaul/paulsha-cortex/issues/497)（work item `fix-superseded-terminal-replay`，Red）的分工：本票只做三件事，全部由既有持久資料導出、不新增 durable 欄位：副作用前的權威 attempt 判定、manifest 覆寫 fence、manifest 內容冪等。#497 保留：
- S01–S04：durable supersession／consumption 的**寫入**、`recover-pre-candidate` 原子清 binding、CAS／request replay、取代與消費路徑接線。
- S07–S09：可達的 recovery fixture，以及完整 `run_tick` 恰派一個新 builder。
- S11：completed slice 重啟後連續 10 tick。
- S12：故障矩陣與 evidence 位址議題。

`complete_tick` 的 slice mutation 與同時發生的 operator action 之間的跨 process CAS 不在本票範圍；R2 只保護 manifest 檔。也不做：#496 dirty recheck、#487 分類、sentinel 收割／targeted complete、manifest 原子寫入、job／evidence GC。不新增 CLI，不改 `dispatch_gate_scan`／`_manifest_still_blocks_fanout`，不改 `_existing_manifest_job_id` 的 None-set 語意。

## Evidence

- #481 本文與 2026-08-12 留言：三個 terminal job 讓同一 exited job 約每 6 秒記一筆 `verification-failed`；recover 回 `pending` 9 秒後被舊 failed job 撥回 failed 並重綁舊 `builder_job_id`；單一 failed job 同樣越過 recovery（`needs_human`／`builder-failed-auth`）。
- Triage 現場實證（2026-09-23 17:41:49，`runtime-pins/cortex-442fe23f`）：單次 tick 改寫 121 份 handoff manifest，恰好等於 `coordinator-cortex/jobs.json` 中擁有兩個以上 terminal job 的 task 數。
- `complete_tick` 的重處理路徑（main `7fa4716b`，`manager.py`）：
  - `:2444` 全量迭代 `registry.list_jobs()`；`:2468` 唯一冪等短路是 `_existing_manifest_job_id(manifest_path) == job_id`，而 `:187-199` 對 passed／verified 與三種 needs_human 回 None。
  - `:2470-2476` 呼叫的 `_slice_for_job`／`_slice_for_reviewer_job`（`:265-285`）對「沒有 slice」與「未綁定」都回 None，於是 `:2592-2604` 對舊 job 寫 `missing-slice-proof`，candidate 由 `_candidate_for_evidence`（`:370-394`）取 branch HEAD。
  - `:2578-2584` failed 路徑呼叫 `update_slice(state="failed")`，而 `registry.py:69` 允許 pending→failed。
  - `:2695-2749` 的 `write_manifest` 整份覆寫、`:2742` 寫 `completed_at: clock()`，把 `_supersede_handoff_manifest`（`:202-235`）補上的三欄丟掉；`:2755-2770` 的 `seen_slices` 只在單輪內去重。
- `apply_slice_action`（`manager.py`）：`:2091-2100` recover 記 `operator-recover-pre-candidate` action；`:2101-2107` 的 `update_slice(builder_job_id=None, candidate=None)` 在 `registry.py:1636`／`:1650` 被當成未提供，binding 不清；`:1999-2027` abandon 記 `operator-abandon`。
- job 建立順序：`registry.py:1039-1047` `_allocate_job_id` 用全域單調 `_seq`，`:1220-1222` `list_jobs` 依建立順序回傳；`manager.py:10936` workflow task `wf-<hash>-<card>` 跨 retry 共用。
- #862 PR #954（head `57f5ceb3`）：`record_job_supersession`／`record_job_consumption` 寫 job 的 `supersession`／`consumption`（含 `binding_revision`、`bound_binding`），`commit_pre_candidate_recovery` 在同一 snapshot 標 supersession 並清 binding；manager 端尚未接線。

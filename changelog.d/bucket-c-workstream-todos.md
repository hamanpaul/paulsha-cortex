# bucket-c-workstream-todos

- **桶C「slice 迴圈家族」workstream 佈線（`#501`／`#497`／`#496`）**——新增三份 todo
  來源並在 `.cortex/work-items.yaml` 註冊對應 work item，讓 cortex 可自行受理這三張
  issue（dogfooding 前置；intake 由 operator 逐案執行）。三張已對 main `48b0205`
  逐條複查，缺陷**全部仍成立**，todo 內含現況查核段與精確檔案行號：
  - `fix-verification-contract-hash-overwrite`（`#501`）：`_apply_verification_result()`
    把 verification **證據** payload hash 經 `update_slice(verification_hash=...)`
    寫進 `slice_row["verification"]["hash"]`，而該欄位是
    `_pinned_input_mismatches()` 賴以比對的 pinned **contract** hash——verification
    一成功、foreign-review 又未即時綁上 reviewer，下一 tick 必然自舉
    `pinned-input-mismatch: verification-hash`。全 repo 僅
    `manager.py:406` 一個呼叫點會這樣寫，修復邊界極窄。現有 slice
    `add-cortex-version-flag-build` 正卡在此現場。
  - `fix-superseded-terminal-replay`（`#497`）：`complete_tick` 全量列舉
    `registry.list_jobs()`，唯一冪等短路是 handoff manifest 的**單槽** job_id，
    同 slice 的其餘歷史 terminal job 每輪重跑；unbound job 更會先以 branch HEAD
    解出 candidate 並寫 `missing-slice-proof`，撞爆 bound job 的不可變證據位址。
    `#383` 只修了 fanout 側（`_manifest_still_blocks_fanout`），completion 側至今
    沒有 supersession 概念，job schema 也無 consumed／attempt 欄位——daemon 正常
    重啟即可讓已 `completed/passed` 的 slice 被回寫並反鎖下游。
  - `fix-dirty-recheck-idempotency`（`#496`）：`candidate-worktree-dirty` 的
    recheck 迴圈（設計意圖，保留）在結果完全未變時仍無條件套用，每 tick 各記
    一筆 `verification-failed` action ＋ evidence_history，實測 116 秒 33 筆。
    既有測試只覆蓋「結果有變」，缺「結果未變不得寫入」的斷言。
- 三張刻意**不合併**：`#497` 是重播來源、`#496` 是 recheck 迴圈、`#501` 是兩者共用
  的污染原語，各自獨立可驗收。每份 todo 的 scope 段明列「本 work item 的主體是 X
  不是 Y」與禁止越界項，供 headless planner／builder 定界。
- 純佈線變更：不改動任何執行路徑程式碼（全套 pytest 3108 passed，與 main 基線一致）。

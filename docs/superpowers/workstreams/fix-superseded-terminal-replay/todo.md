---
status: accepted
work_item: fix-superseded-terminal-replay
---

# fix-superseded-terminal-replay Todo

`#497`：**已被取代（superseded／unbound）的 terminal job 會被 `complete_tick`
反覆重新終局化**，並在「slice_id + candidate SHA」這個決定性證據位址上撞上
不可變證據寫入器，fail-closed 阻斷本輪 fanout。實測後果包含：復原後不派新
builder、`completed/passed` 的 slice 被回寫成 `needs_human`、下游 slice 被
`deps-unsatisfied` 反向鎖死。

## 現況查核（0816，對 main `48b0205`）

**缺陷仍成立。`#383` 只修了 fanout 側，completion 側完全沒有 supersession 概念。**

已落地的部分（不要重做）：

- `manager.py:2194-2215` `_manifest_still_blocks_fanout()`：fanout 放行改與
  registry 現況對帳，復原成 `pending` 的 slice 不再被殘留 manifest 永久跳過。
- `manager.py:164-199` `_supersede_handoff_manifest()`：復原動作後替 manifest 補
  `superseded_at`／`superseded_by`／`superseded_reason` 稽核欄位。

**沒修到的部分（本 work item 的主體）**：

1. `manager.py:1855` —— `complete_tick` 直接 `for snapshot in registry.list_jobs():`
   **全量列舉**所有 job，對每個 terminal job 嘗試終局化。沒有任何
   supersession／attempt 過濾。
2. `manager.py:1879` —— 唯一的冪等短路是
   `if _existing_manifest_job_id(manifest_path) == job_id: continue`。
   這是**每 slice 單槽**的記憶：manifest 只記得住**一個** job_id。同一 slice
   歷史上若有 `-6`／`-8`／`-9` 三個 terminal job，manifest 指向 `-9` 時，
   `-6`／`-8` 完全不受保護，每輪都重跑。
3. `manager.py:154-161` —— `_existing_manifest_job_id()` 另外在
   `gate_status in {"passed","verified"}`、以及
   `needs_human + verification_evidence_path is None + gate_reason ∈
   {pinned-input-mismatch, verification-runner-error, verification-state-update-error}`
   時**主動回 None**（＝放行重播）。證據寫入撞衝突時 `evidence` 留 None，正好落進
   第二個條件，形成穩定的每 tick 重播迴圈。
4. `manager.py:226-236` `_slice_for_job()` 在 `builder_job_id != job_id` 時回 None，
   但**回 None 不等於跳過**：build lane 會掉進 `manager.py:1997-2010` 的
   `missing-slice-proof` 分支，該分支**照樣寫證據**——且 candidate 由
   `_candidate_for_evidence()`（`manager.py:285-309`）以 **branch 當前 HEAD**
   解析。這就是 `#497` 0812-18:05 現場的機制：舊的 unbound job `-8` 先被終局化，
   用 branch HEAD 解出 `6421bc98…`，把 `missing-slice-proof` 寫進本該屬於
   bound job `-9` 的證據位址；`-9` 隨後撞
   `conflicting verification evidence … (content mismatch)`。
5. `registry.create_job()`（`registry.py:873-900`）的 job schema **沒有任何**
   consumed／superseded／attempt 欄位，因此 supersession 目前根本無處持久化。
6. `manager.py:1477-1560` `recover-pre-candidate`：把 slice 撥回 `pending`、
   `builder_job_id=None`、`candidate=None`，並標記 manifest——但**舊 job row
   本身完全沒動**，仍是 `list_jobs()` 的合法 terminal 成員。
7. `verification.py:377-397` `_existing_evidence_result_or_raise()`：同位址內容不同時
   把原檔 `os.replace` 進 `quarantine/` 後 `raise RuntimeError`。**這一段是對的
   （不可變證據 fail-closed），不要為了消 symptom 去放寬它。**

## Scope（明確邊界）

**本 work item 的主體是「completion 側的 attempt 身分與 supersession 過濾」，
不是「證據不可變規則放寬」，也不是「證據位址重設計以外的其他一切」。**

- 要做：terminal job 在被終局化**之前**先判定它是否仍是該 slice 當前 attempt 的
  綁定 job；不是就跳過——且**必須在「解析 candidate／推導證據位址」之前**跳過，
  否則就像現況一樣，已經寫壞了才發現。
- 要做：supersession 需**持久化在 registry**（job row 或 attempt 層），
  不能只靠 handoff manifest 的單槽 job_id 或檔案系統狀態——`#497` 的
  0812-19:10 現場證明 daemon 重啟後重播照樣發生。
- 要做：舊 job 保持**可稽核**（不刪 row、不刪既有證據），只是不再有資格成為
  當前 attempt 的終局。
- 可做（若判定必要）：在證據位址中納入 job／attempt 身分，讓「同 slice 同 SHA
  的多次合法終局觀測」各有位址——這是 `#497` 建議驗收的第 4 條。若採此路，
  **既有證據路徑必須保持可讀**（completion record 會引用舊位址，
  見 `manager.py:843` `verification_evidence_path`）。
- **不要做**：放寬 `write_verification_evidence()` 的不可變性、或把 quarantine
  改成靜默覆寫。fail-closed 抓到的是真問題，本修復要消除的是「不該發生的
  第二次寫入」，不是「抓到衝突時的反應」。
- **不要做**：修 `#501` 的 contract／evidence hash 欄位混用，或 `#496` 的
  dirty recheck 冪等。本張假設 `#501` 可能尚未落地，因此**驗收不得依賴
  contract hash 正確性**；若 `#501` 已先行 merge，測試可一併收緊。
- **不要做**：改動 `_manifest_still_blocks_fanout()`／`dispatch_gate_scan()`
  的 fanout 語意（`#383` 已定案且有測試）。本張只碰 completion 側。
- **不要做**：新增自動 retry／自動修復。`needs_human` 在 operator 動作前必須
  **靜止**（quiescent），這正是驗收條件之一。

## Tasks

- [ ] **attempt 身分持久化**：registry 提供「此 terminal job 已被消費／已被取代」
      的持久標記（CAS 更新，restart-safe），並在
      `recover-pre-candidate`／`abandon`／新 attempt 派工時原子地設定
- [ ] **completion 前置過濾**：`complete_tick`（`manager.py:1855` 起）在**推導
      candidate 與證據位址之前**先跳過非當前 attempt 的 terminal job；
      `_slice_for_job()` 回 None 的 build-lane 情境不得再無條件寫
      `missing-slice-proof` 證據（`manager.py:1997-2010`）
- [ ] **單槽記憶除役**：不再以 handoff manifest 的單一 `job_id`
      （`manager.py:1879`／`_existing_manifest_job_id`）作為多 terminal job 的
      冪等權威；改以持久 attempt 標記為準
- [ ] **completion 證明穩定性**：已 `completed/passed` 且持有合法 completion record
      的 slice，其被引用的 verification evidence 位址在 daemon 重啟後不得被任何
      重播改寫（`#497` 0812-19:10 現場）
- [ ] **測試**：
      - terminal dirty builder → `recover-pre-candidate` → tick：舊 job 被跳過、
        無證據衝突、**恰好**派出一個新 builder
      - 上述情境的 **daemon restart 變體**：跳過語意跨重啟存活
      - 同 slice 多 terminal job（`-6`／`-8`／`-9`）：只有當前綁定的 job 被終局化，
        unbound job 不寫任何證據、不解析 branch HEAD 當 candidate
      - `completed/passed` slice 在 manager 重啟後連續多 tick：completion record
        與其引用證據 byte 不變、下游 slice 不被 `deps-unsatisfied` 反向鎖
      - `needs_human` 靜止性：連續 tick 不新增 action／evidence_history
        （與 `#496` 的驗收互補，但本張測的是「重播來源」而非「recheck 迴圈」）
      - 舊證據與舊 job row 仍可稽核讀取（不因修復而遺失歷史）

## 現場紀錄（供實作者參考）

- issue `#497` 首則：Task 3 `recover-pre-candidate` 後首個 tick 未派工，
  改為重跑舊 terminal builder 並撞 `conflicting verification evidence … (content mismatch)`
- 0812-14:57 留言：recovery 回 `slice_state: pending` 後 8 秒，terminal reviewer
  `-2` 被重跑，slice 退回 `needs_human / missing-slice-proof`
- 0812-16:11 留言：重複 recovery 每次都被同一個 terminal job 打回，
  「pending 無法存活」——明確要求 consumed／superseded marker CAS
- 0812-17:25 留言：foreign-review launch 失敗後，約 90 秒累積 **24 筆**
  evidence_history（本張是重播來源，`#496`／`#501` 是放大器）
- 0812-18:05 留言：unbound job `-8` 先於 bound job `-9` 被終局化，
  造成同 candidate `6421bc98…` 證據位址衝突與兩份 quarantine 檔——
  這則最精確地指出「必須在解析 candidate 之前跳過」
- 0812-19:10 留言：**daemon 正常重啟**即可讓已 `completed/passed` 的 Task 4
  被重播回寫成 305-byte 的 `missing-slice-proof`，handoff 被改寫為
  `completion-record-missing`，Task 5 被 `deps-unsatisfied` 反鎖——
  證明缺陷不限於 recovery 競態
- 0812-19:40／21:09 留言：狀態快照內部不一致（`completed/passed` 併
  `reason=completion-record-missing`）；以及 manager interval 被夾到 3600 秒
  當 workaround 後，`stat`／`status` 無法收割已存在的 exit sentinel。
  **後者（sentinel 收割／targeted `complete <job-id>`）屬觀測面 follow-up，
  不在本張 scope 內**——本張只負責讓重播不再發生

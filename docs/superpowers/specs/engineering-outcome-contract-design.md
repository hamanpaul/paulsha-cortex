---
status: accepted
work_item: engineering-outcome-contract
---

# engineering-outcome-contract Design

## Decisions

### D1 outcome 詞彙縮限到有既有終局轉換點的兩種

issue 草案假設六種 outcome：`shipped`／`verified`／`rejected`／`failed`／
`abandoned`／`rolled_back`。查證 `registry.py` 後發現 `WorkflowRun.status` 只有
`ongoing`／`done`／`superseded` 三個合法值，`failed` 只存在於 job 層
（`VALID_JOB_STATUSES`），不是 run 級終局。

`OUTCOME_STATUSES` 因此保留全部六種（`verified` 併入既有的 `passed`/`shipped`
語意不重複定義，故 schema 仍以 issue 原文的五種扣除 `verified` 為準：
`shipped`／`abandoned`／`rejected`／`failed`／`rolled_back`）作為 schema 合法值
全集，但 v1 只在 `_ship_action`（`status="done"`）與 `_abandon_action`
（`status="superseded"`）兩個既有終局轉換點呼叫 `emit_outcome`。

理由：`rejected`／`failed`（run 級）／`rolled_back` 目前靠 `needs_human` facet
卡住、由人工決定下一步，不是一個「run 已終結且不可逆」的自動轉換；為了湊滿六種
狀態去發明新的 run-level 終局狀態，等於在一張「發布 outbox」的票裡順手改動
lifecycle 狀態機的核心不變量，範圍失控且缺乏獨立驗收。schema 保留這些值只是為了
未來擴張既有終局轉換點時，消費端不需要處理一個新的 `kind`。

### D2 outcome_id 由呼叫端提供的 attempt_digest 決定性推導

`outcome_id(run_id, outcome, attempt_digest)` 純函式，SHA-256 取前 20 hex，不含
任何時間戳或隨機性。`attempt_digest` 由呼叫端提供，且刻意選用「這次終局轉換本來
就會產生的內容位址 digest」，不新增額外的 digest 計算：

- ship：`closure.completion_record["hash"]`——同一次 merge 重跑 ship（daemon
  restart、request retry）會重新驗證出同一份 completion record，故 hash 相同。
- abandon：`_abandon_record(...)["hash"]`——abandon evidence body 的內容 digest；
  `run.status == "superseded"` 的重入分支會從既有 evidence 檔重讀回同一份
  `body`（`_superseded_abandon_body` 驗證讀回內容與檔名的 digest 一致），因此重
  入呼叫算出的 digest 與第一次相同。

理由：避免另外維護一份 idempotency 索引或 nonce——「這次轉換的內容本身就是它的
身分」，兩次呼叫如果內容真的相同（同一次轉換），digest 就相同，`outcome_id` 就
相同，`OutcomeStore.append` 據此去重。如果內容不同（例如換了一次不同的 merge
commit），digest 不同，會被視為新的一筆 outcome——這是刻意的：不同次轉換不應該
互相覆蓋或吞掉彼此。

### D3 append-only、一 repo 一檔的 JSONL outbox

`OutcomeStore` 持有 `<state_root>/engineering-outcomes/<repo-slug>.jsonl`，每次
`append` 整檔讀回、依 `outcome_id` 去重、整檔以 `tempfile.mkstemp` +
`os.replace` + `os.fsync`（含 fsync 目錄）重寫。

理由：

- **一 repo 一檔，不是一 work_id 一檔**：後者會隨 work item 數量開出大量小檔，長
  期造成 fsync 爆量與 inode 增長；前者把同一 repo 的所有 outcome 收斂進同一份可
  tail 的 JSONL，append 頻率與終局轉換次數同數量級（ship／abandon 本身不頻繁，
  不是高頻事件）。
- **整檔重寫而非真正的 append-only 檔案 I/O**：idempotency 檢查需要先知道
  `outcome_id` 是否已存在，最簡單可靠的做法是讀回全部既有 record 掃一輪；record
  量體小、事件頻率低，整檔重寫的成本可忽略，換取的是不需要另外維護一份索引檔
  （索引檔本身也要處理一致性，等於多一個要保證原子性的地方）。
- **atomic write 手法比照 `registry.JobRegistry._write_payload_atomically`**：
  寫暫存檔→fsync→`os.replace`→fsync 目錄，崩潰時原始檔案维持不變（`os.replace`
  是單一系統呼叫）。不採用 `JobRegistry` 那套額外的 backup-and-rollback 邏輯，因
  為 `OutcomeStore` 是無狀態的（每次操作都重新讀檔），不像 `JobRegistry` 持有長
  駐記憶體狀態需要在寫入失敗後回滾到一致快照。

### D4 execution_provenance 明確標示 correlation_confidence: "weak"

Cortex job record（`registry.create_job` 的欄位集合）沒有存 executor 自身的
session UUID，只有 `session_name`（慣例上等於 slice 的 task id）、`log_path`、
`pane`。`_build_execution_provenance` 因此只組出 `worktree_root`（run 的
`workspace_root`）＋`time_window`（run 的 `created_at`/`updated_at`）＋
`session_refs`（job 的 `session_name` 集合），並顯式標記
`correlation_confidence: "weak"`。

理由：這是 Hippo 在 issue comment（2026-08-06）指出的具體落差——如果不誠實標示，
消費端會誤以為這是可以拿去做 exact session 比對的強索引。捕捉真正的 executor
session id 需要改 `dispatcher.py` 在派工當下把 session id 寫回 job record，這是
比本票大的另一項變更，此處只記 follow-up，不在本票範圍內動手。

### D5 jobs 欄位展開成 per-job 物件

`_project_jobs` 把 `emit_outcome` 收到的 job record 過濾（依
`workflow_run_id`）＋投影成 `{job_id, card, persona, workflow_phase}`，而非 issue
草案的扁平 job id 字串陣列。

理由：這幾個欄位 job record 本來就有（`workflow_card`／`persona`／
`workflow_phase`），公開成本低；消費端（例如做「這個 slice 是哪個 persona／哪張
card 產出的」分析）不需要再反查一次 registry。同時採納 Hippo 在 issue comment 提
出的第二點修正意見。

### D6 終局轉換順序：先 emit_outcome，再改 WorkflowRun.status

`_ship_action`／`_abandon_action` 在呼叫 `_manager_update_workflow_run`／
`_manager_abandon_workflow_run` **之前**呼叫 `emit_outcome`。若
`OutcomeStore.append` 拋例外（例如磁碟寫入失敗），terminal transition 不會執行，
例外原樣往上傳播——不新增額外的補償或重試邏輯，維持既有的「未捕捉例外即代表這次
呼叫沒有完成、下次 retry 會重新走一次」語意，且因為 D2 的 idempotency 設計，重試
不會產生重複 record。

理由：滿足 issue 的硬性要求「terminal transition 前先 durable 寫入」，讓
「outcome 已記錄」永遠不會落後於「WorkflowRun 已終結」——反過來（run 已終結但
outcome 遺漏）才是需要避免的 fail-open 缺口；`emit_outcome` 失敗導致整個 action
失敗是刻意的保守選擇，優於「terminal transition 成功但 outcome 悄悄遺漏」。

## 風險與緩解

- **`_abandon_action` 的 superseded 重入分支重複呼叫 `emit_outcome`**：刻意保
  留（不是 bug）——這個分支本來就是為了覆蓋「第一次 emit 之後、terminal
  transition 之前 daemon crash」的窗口而存在；D2 的 idempotency 設計讓重入呼叫
  是安全的 no-op（`OutcomeStore.append` 偵測到相同 `outcome_id` 直接回傳既有
  record，不重寫檔案）。
- **`OutcomeStore.append` 整檔重寫在 outcome 數量很大時會變慢**：目前的事件頻
  率（每個 work item 一生只會 ship 或 abandon 有限次）不構成問題；若未來需要支
  撐高頻寫入，可以在不破壞既有讀取 API（`list_outcomes`/`show_outcome`/
  `replay_outcomes`）的前提下換成真正的 append-only 檔案 I/O（fadvise/seek to
  end）＋獨立索引檔，是純粹的內部實作優化，不影響 outbox 的檔案格式或
  schema。
- **schema 保留 `rejected`／`failed`／`rolled_back` 卻無 emitter，消費端可能誤
  以為已支援**：`EMITTED_OUTCOME_STATUSES` 常數與本文件（D1）明確記錄哪些狀態
  目前有 emitter，且 v1 的 CLI／文件都只示範 `shipped`／`abandoned` 的實例。

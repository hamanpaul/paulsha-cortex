### Added

- **task memory「使用」層的正式回報路徑**（#1136）：卡片 terminal 新增選填欄位 `task_memory_applied`（至多 3 筆 `{"note_id", "evidence_ref", "evidence_sha256"}`），harvest 採信 terminal 之後才處理，讓 #857 的 `applied-with-evidence` receipt 首次有正式呼叫者。
  - **terminal 欄位**：`task_memory.parse_task_memory_applied()` 嚴格驗證（list、上限 3 筆、恰好三個字串鍵、note id 格式且不重複、正規化 repo 相對路徑且不含 `..`／`.git`／控制字元、小寫 64-hex）；`manager._extract_terminal_json()` 在所有既有形狀判定之前拆掉這個欄位，因此帶或不帶、合法或畸形，malformed／明示停止分類、gate 矛盾偵測與 canonical evidence 都逐位元組不變。Claude／AGY reviewer 的工具 schema 開放此屬性（`task_memory.task_memory_applied_json_schema()`，Gemini 相容子集）。
  - **harvest 接線**：`terminalize_workflow_job()` 在 canonical evidence 綁定與 report 提交之後呼叫 `_harvest_task_memory_applied()`：以本 attempt 的正式 Work Item／Run／card／Job 邊界重建 `TaskMemoryContext`，並由 `task_memory.restore_prepared_task_memory()` 從 Manager receipt sidecar 重建本 attempt 已 offer（candidate-selected＋offer-emitted）與已交付（content-returned／context-delivered）的 note——dispatch 時的 prepared context 不另外持久化，也不含 note 內容；逐筆 `record_applied()` 後經 `record_task_memory_receipt()` 寫入。builder 另以 `git cat-file blob <candidate>:<ref>` 證明 evidence 在本 attempt candidate commit 內；`record_task_memory_receipt()` 的 `_verify_applied_artifact` 改依 persona 選根目錄（reviewer 用審查對象 `workflow_repo_root`，不再用用完即丟的 sandbox）。
  - **失敗只留診斷**：欄位畸形、phase 不支援、note 未交付或屬於別的 attempt、evidence 不在 candidate、hash 不符都只記 class-only `logger.warning`，不擲例外、不改 job／run／gate；一筆失敗不影響同一 terminal 的其他筆，重跑冪等。
  - **prompt**：task-memory inline 區塊只在 build／verify／review 卡且確實交付 note 時，說明只有依 note 內容實際改變產出才回報、並附 candidate 檔案的 repo 相對路徑與 sha256；其餘 prompt 逐字不變。
  - **KPI**：applied receipt 的 `counts_as_read` 恆為 false，canary／legacy strict KPI 分母不變（#857 R5）。
  - **已知限制**：三分帳號部署下 Manager 讀不到 builder 工作區，builder 的 applied evidence 會被拒收（只留診斷）；live daemon 驗收尚未執行。
  - 新增 `tests/test_task_memory_applied_evidence_1136.py`。

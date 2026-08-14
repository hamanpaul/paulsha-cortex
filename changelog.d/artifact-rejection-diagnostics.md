---
type: fix
scope: coordinator
---
**Issue #511（診斷面，前兩項）：planning artifact 被拒時的原因與內容可觀測**

`coordinator/planning.py` 的 `assess_planning_artifact()` 一直都回傳完整的
`ArtifactAssessment(artifact, accepted, reasons, blocking_markers)`，但
`coordinator/manager.py` 的 `_publish_planning_artifacts()` 只取布林值：

```python
if not assess_planning_artifact(artifact).accepted:
    raise ValueError(f"planning artifact is not accepted: {path_value}")
```

`reasons`（`status-not-accepted`／`required-section-missing`／`blocking-decision`）
與 `blocking_markers`（行號＋原文）全被丟棄。被拒的 artifact 內容又只活在
planning launcher 的 `tempfile.TemporaryDirectory()` 裡（`planning_runtime.py`
的 `last.json`），context 一結束就刪除，沒有任何副本留存。結果是 operator 拿到
的唯一訊息只有一句「不被接受」——不知道是哪一條驗收條件不過，也看不到 planner
究竟寫了什麼，只能盲目重試（實測 abandon→重新 claim→同樣失敗，四次全同），
Phase 1 派工因此死鎖。

- **A. 拒收原因進錯誤訊息**：訊息改為
  `planning artifact is not accepted: <path> (reasons=...; markers=Lnn:...; evidence=...)`。
  `blocking-decision` 會附上 markers 的行號與截斷後文字（最多 3 條，其餘以 `+N`
  帶過，單條上限 48 字）。訊息保證單行——它會被 `run_heterogeneous_brainstorm`
  包進 needs_human reason、原樣落進 `cortex-planning-failure/v1` evidence 的
  `reason` 欄位，而 `work_actions`／`control.contract` 對 `failure_reason` 明確
  拒收換行；長度上限 `PLANNING_ARTIFACT_REJECTION_MESSAGE_MAX_LENGTH = 400`
  （比照 `manager_daemon.TICK_ERROR_REASON_MAX_LENGTH` 的截斷補 `…` 作法，但放寬
  到能容下路徑＋markers＋evidence 路徑）。欄位順序刻意排成 reasons → markers →
  evidence：上游 `planning.py` 對 artifact-write 例外另有 `str(exc)[:160]` 的
  截斷，最短且最關鍵的 reasons 先寫才保證存活；完整訊息另以 `logger.error`
  （`planning-artifact-rejected`）落一筆 log。
- **B. 被拒 artifact 內容落 evidence**：新增 `cortex-planning-artifact-rejection/v1`
  schema，寫到 `<coordinator_root>/evidence/planning-artifacts/{run_id}-{digest}.json`，
  含 `kind`／`path`／完整 `content`／`reasons`／`markers`／`work_id`／`run_id`／
  `created_at`。原子寫入與檔名慣例（canonical json hash、tmp→fsync→rename→0400、
  內容衝突 raise）比照既有 `_write_planning_failure_evidence` 與
  `work_actions._recover_planning_record`，不另創風格。內容上限 64K 字元，超過
  即截斷並標記 `truncated: true` 與原始長度 `content_length`，避免 evidence 爆量。
  evidence 記錄本身 fail-open（比照 `_record_planning_failure_evidence`）：落檔
  失敗只留 log，拒收本身仍照舊 raise，不會把真正的拒收原因換成 IO 錯誤。

兩個刻意的落點取捨：

- **目錄與 `planning-recovery` 並列而非混用**：`work_actions._read_planning_failure_record`
  用 `path.parent.name == "planning-recovery"` 當 recover-planning 的收容判準，
  把 rejection evidence 塞進去會多出一筆無法解析的候選、撞上
  `planning failure evidence ambiguous` 的 fail-closed。兩份 evidence 共用同一組
  `run_id` 前綴，operator 可用同一個 run_id 交叉撈。
- **evidence 落 `coordinator_root` 而非 `artifact_root`**：後者是被 cortex daemon
  監控的 operator worktree，planning 失敗時會整棵樹抹除再從 baseline 還原
  （#507），診斷落在那裡等於白記。define 路徑的 `artifact_writer` 因此改帶
  `coordinator_root=transaction_root`，與 `_record_planning_failure_evidence` 同一個 root。

本次不含（後續票）：`blocking-decision` 的回答通道／新 CLI 動作、`content` vs
`environment` 的分類改判、自動暫停 auto-claim、recover-planning 放行條件。

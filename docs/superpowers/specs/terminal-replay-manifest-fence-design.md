---
status: accepted
work_item: terminal-replay-manifest-fence
---

# `complete_tick` 權威 attempt 判定與 handoff manifest 覆寫 fence 設計

## Decisions

### D1 Admission 是副作用前的 pure 判定

新增 pure helper `_terminal_job_admission(job, *, slice_row, job_order, latest_terminal_order, manifest_payload) -> str | None`，回傳 skip reason（`job-superseded`／`job-consumed`／`non-current-attempt`／`operator-fence`／`newer-terminal-attempt`／`manifest-fenced`），None 代表放行。helper 本身不做 I/O，只吃已讀出的資料：job dict、slice row（查無為 None）、snapshot 序位、各 task 最晚 terminal 序位、manifest payload。

`complete_tick` 在 manifest symlink 檢查之後，先以 `registry.get_slice(slice_id)`（`KeyError` → None）與 `_read_manifest_payload(manifest_path)` 取得資料，再呼叫 helper；命中即 `continue`。skip reason 只寫 `logger.debug`，不進 summary，避免每 tick 重複輸出。

放行之後的流程逐字沿用現行程式：`_existing_manifest_job_id` 短路、`_slice_for_job`／`_slice_for_reviewer_job`、builder 已有 reviewer 時 `continue`、各 gate 分支。因為 (b) 已先擋下「slice 存在但未綁定」，`slice_row is None` 的分支從此只剩「真的沒有 slice row」的 job 會走到：workflow lane，或真正缺 slice。

### D2 Snapshot 序位即建立順序

迴圈前取一次 `snapshots = registry.list_jobs()`，迴圈改為迭代這份 list（原本是直接迭代 `registry.list_jobs()`）。由它建立兩個索引：`job_order = {job_id: index}`，以及 `latest_terminal_order = {task: 該 task 在 snapshot 中狀態屬 TERMINAL_STATUSES 的最大 index}`。

序位就是建立順序：`_allocate_job_id` 用全域單調 `_seq`，jobs 只 append。因此不比 job_id 字串，也不比時間戳。

本輪才 poll 成 terminal 的較新 job 不回溯影響較舊 job 的判定，因為它在迴圈中排在後面。所以同輪雙 terminal 維持既有行為：兩者依序寫、後者勝，並記 `same-slice concurrent terminals` warning。

(d) 跳過的較舊 failed attempt 不再經 `complete_tick` 記 executor backoff。它的終局仍在 registry 的 terminal inventory 裡，會由 admission 的 backoff 重播（`manager.py:4544` 起）補 ack；workflow lane 另有 `manager.py:11866` 的記錄點。

### D3 Operator fence 以 registry action 為準

新增常數 `_OPERATOR_FENCE_ACTIONS = frozenset({"operator-recover-pre-candidate", "operator-abandon"})`。fence 時刻取 slice `actions[*].at`，與 job `created_at` 比較。`at` 由 registry `_now_iso()` 產生、append-only、restart 後仍可讀。兩個欄位都以 `datetime.fromisoformat` 解析，naive 值視為 UTC；解析不了就算命中（寧可 audit-only，operator 仍能 `retry-build`）。

以下兩種都不採用：
- 以 manifest `superseded_at` 當唯一依據：manifest 可能缺檔，也可能被覆寫。
- 以 slice `state == "pending"` 當 fence：既有測試（例如 `tests/test_pinned_spec_delivery_503.py`、`tests/test_repo_root_fail_closed_612.py`）用 `create_slice(builder_job_id=...)` 建立 pending 且已綁定的 fixture，並期待它被處理；況且 pending 不等於經過 operator 復原。

#497／#862 接線之後，若 recover 改走 `commit_pre_candidate_recovery`，舊 job 會帶 `supersession`，由 (a) 接手；(c) 留作 legacy recover／abandon 路徑的 fence，兩者不衝突。

### D4 Manifest fence：admission 與寫入前各判一次

新增 pure helper `_manifest_fences_job(payload, job_id, job_order) -> bool`，實作 R2：(i) `superseded_at` 非 null 且 `job_id` 等於本 job；(ii) `job_id` 在 `job_order` 中的序位大於本 job。admission 時用一次；`handoff.write_manifest` 之前，重新 `_read_manifest_payload` 再用一次。

寫入前才命中時，只影響 manifest：本 job 在此之前已經完成的 evidence 寫入與 slice mutation 不回滾（跨 process CAS 屬 #497／#862）。但這一步保證舊 job 不會把較新或 superseded 的 manifest 蓋掉，並記 `manifest-write-fenced` warning 讓 operator 看得到。這個 job 不進 `completed`／`completed_jobs`，也不更新 `seen_slices`。

### D5 內容冪等只省檔案寫入

新增 pure helper `_manifest_payload_equivalent(existing, new) -> bool`。兩份 payload 各自經 `json.loads(json.dumps(..., sort_keys=True, ensure_ascii=False))` 正規化並去掉 `completed_at` 後相等，且 existing 的 `job_id` 相同、沒有 `superseded_at`，才算等價。

等價時不呼叫 `handoff.write_manifest`。`_discard_unpublished_evidence`、`completed`／`completed_jobs`／`seen_slices` 的更新順序與內容維持現行，所以 None-set 重處理的回報語意不變，只是不再每 tick 重寫 `completed_at`。

### D6 #862 disposition 只讀

判定條件是 `isinstance(job.get("supersession"), Mapping) or isinstance(job.get("consumption"), Mapping)`。本票不呼叫 `record_job_supersession`／`record_job_consumption`／`commit_pre_candidate_recovery`，寫入與 producer 接線屬 #497 child B／C；也不讀 `binding_revision`，因為 (b) 比對綁定現值就足以判定現行 attempt。

測試以 #862 merge 後的公開 API 產生 disposition：在 versioned slice 上，對目前 `binding_revision` 已綁定的 job 呼叫 `record_job_supersession`／`record_job_consumption`。若 merge 版的簽名與 PR #954 head 不同，以 main 為準。

### D7 與 #497 的切分

本票落地的是 #497 S05／S06／S10 中「非現行 attempt 在副作用前 skip」這一段，外加 #481 自己要求的兩項：manifest 覆寫 fence、內容冪等。#497 的 durable disposition 寫入、原子 recovery、CAS／replay、consumption 時機、restart 故障矩陣、evidence 位址仍在 `fix-superseded-terminal-replay`；該票後續可沿用本票 helper，不需重做判定。

### D8 風險／測試矩陣

| Surface／風險 | Harness | Oracle |
|---|---|---|
| slice lane A/B/C ping-pong | 以 `tests/test_coordinator_manager.py` 的 `_create_slice` 建 slice（fake repo，spec／plan 為絕對路徑且 hash 相符），並把 `PSC_REPO_ROOT` 設到該 repo（或用 `tests/test_pre_candidate_recovery.py` 的 `_seed_repo`）；A exited、B failed，皆未綁定；C exited 且綁定（state `building`），`verification_runner` stub 以 `verification.write_verification_evidence` 對 C 寫入並回傳 status `needs_human` 的 evidence（記為 `verification-failed` action）；`git_runner` 對 A 的 branch 回傳與 C candidate 不同的 SHA，好區分兩者的 evidence 檔；clock 每 tick 不同 | tick1 `errors == []`、manifest 指 C、`actions` 恰一筆 `verification-failed`；A 的 branch 不觸發 `rev-parse`，`evidence/verification/` 無 A 的 `missing-slice-proof` 產物；tick2 manifest sha256、`actions` 長度、evidence refs、state、gate 全不變（修前 tick2 會重跑 C 的 verification，多一筆 action） |
| recover 前後舊 job 回灌 | 另建 fixture：三個 failed builder、slice 綁 C、state `failed`、`candidate=None`（上一列的 C 驗證後 candidate 已是 SHA，recover 不被允許）；先跑兩次 tick，再 `apply_slice_action(action="recover-pre-candidate")` 回 `ok` | recover 前兩次 tick 的 manifest sha256 不變；recover 後兩次 tick slice 為 `pending`/`pending`，`builder_job_id` 與 recover 當下相同，manifest 的 superseded 三欄保留且 bytes 不變，`completed`／`errors` 不含 A/B/C |
| None-set manifest＋缺檔＋restart | 單一 failed job、slice `needs_human`、`candidate=None`，manifest 為 `needs_human`＋`verification_evidence_path: null`＋`pinned-input-mismatch`（failed job 可達的 None-set：mismatch 分支先於 failed 分支，`_write_status_evidence` 回 None 時 evidence path 為 null）；recover 後另測刪除 manifest（job 在 recover 前從未終局化）、以同 `state_path` 重建 `JobRegistry` | slice 不被撥回 failed／needs_human，不寫新 manifest（缺檔變體維持缺檔） |
| workflow lane 多 attempt | `tests/test_coordinator_manager.py` 的 `_make_workflow_job` 樣板建立同 task 三個 job，全部 terminal | tick1 只有最新 job 寫 manifest；tick2 bytes／mtime 不變、`completed == []` |
| deferred write fence | `verification_runner` 在執行中把 manifest 改寫成指向本 job 的 superseded payload 後 raise | manifest bytes 等於被改寫的內容，`warnings` 含 `manifest-write-fenced`，`completed` 不含該 slice |
| manifest 指向較新 job | 無 slice row、同 task 兩個 job，較新者仍在飛（poll 不轉態），manifest 預寫指向較新者 | 較舊 terminal job 不覆寫 manifest |
| #862 disposition | versioned slice 綁定 J，對 J 呼叫 `record_job_supersession`（另一例 `record_job_consumption`），manifest 缺檔 | J 不處理、不寫 manifest，slice 欄位不變 |
| fence 後新 attempt | recover 後建立 D 並 `update_slice(state="building", builder_job_id=D)`，D failed | D 終局化並覆寫 superseded manifest，A/B/C 仍 skip |
| 內容冪等 | verified／passed slice、builder 綁定、無 reviewer、`review_policy: not-required`，`git_runner` 讓 `merge-base --is-ancestor` 回 1 | 兩輪 gate 皆為 `verified`／`candidate-not-merged`；第二輪 manifest bytes／mtime 不變、`completed_at` 保留 |
| 同輪雙 terminal | 既有 `test_concurrent_same_slice_terminals_warn_and_dedup` | 原斷言通過 |

所有 RED 案例在修前必須因重播而失敗，不能因 repo root 未設定或 spec 不可讀提前落入 `errors` 而假綠。

### D9 Sizing

只改 1 個 production 模組（`manager.py`）→ `domain_breadth=0`。判定只讀既有 registry 持久欄位（binding、actions、#862 disposition）與 manifest，改動只是抑制既有的單檔寫入，不新增 durable 欄位、不做跨物件 CAS → `state_consistency=1`。三件齊全時機械三維固定 4，總分 5／Yellow。

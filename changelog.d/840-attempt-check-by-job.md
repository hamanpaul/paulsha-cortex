# #840 attempt 判定改以 job 事實，不再依賴 step executor/model（refine R10 對抗審查第四輪）

live 驗收（9900X user 級 daemon，真實 run `workflow-08ca9b35fc7955942a21`）發現：
`tdd-red` 卡的 builder job 已派出並在執行（`status=dispatched`），
`WorkflowRun.quota_admission.builder` 指向本次 attempt 的 shadow admit
receipt，但 `WorkflowStep(tdd-red).executor`／`model` 仍為 `None`——正式派工
路徑（`_dispatch_workflow_card`）寫入 admission receipt 與寫入 step 身分
（`_record_resolved_model_chain()`，job 建立、dispatch 之後才落地）不是同一
個時間點，中間有一段窗口。`decision_projection._attempt_mismatch_reason` 的
admit 分支過去比對 step 的 `executor`／`model` 是否與 `decision.selected`
一致，把這段窗口誤判成『身分已被 retry-card 清空』，導致所有進行中的
attempt 都被 `cortex work show`／`cortex inspect status` 誤呈現成
`available:false`、`gap_reason: quota-decision-attempt-superseded`。

改以 job 事實判定：`decision.attempt_id`（格式
`f"{run_id}:{card_id}:n{k}"`，可能帶 `:g{generation}` 世代後綴）的 0-based
ordinal `k` 反查該 run/card 依建立順序的第 `k` 個 job——沿用 #839 對抗審查
第四輪之前、`_quota_admission_job_lookup_by_attempt`（已被
`_quota_admission_job_lookup_by_decision` 取代）記載的同一套解析規則，不
另寫第二套；那支函式被取代的理由（ordinal 猜測在多 Manager instance 交錯
時會誤配 reservation 歸屬）不適用於本模組——唯讀 status 投影不做
reservation 收斂，答錯了也只是呈現面暫時多顯示或少顯示一筆決策。判定規則：

- job 存在且未終局（沿用既有 `manager.IN_FLIGHT_STATUSES` 定義）→ 當前，
  完整呈現。
- job 已終局或查無此 job（且卡片尚未 passed，由既有 `card` 比對保證）→
  「本 attempt 已結束、下一個 attempt 尚無決策」，沿用既有
  `quota-decision-attempt-superseded` gap_reason，`mismatch_reason` 改為
  精確字串 `decision-attempt-ended`（取代不再可靠的
  `identity-reset-since-decision`）。
- decision 的 `card_id` 與目前卡不同 → 維持既有 `card-superseded`。
- 呼叫端沒有能力提供 job 事實（省略 `jobs` 參數）→ 不做 attempt 判定，
  不臆測，維持逐字既有行為。
- wait decision（從未寫入 step 身分）維持上一輪以 `needs_human_reason` 判定
  的規則，不受本次修法影響。

新增 `decision_projection.jobs_for_run_from_rows()`：`cortex work show`
（`WorkflowRegistryProvider.scan()`，job rows 來自 registry payload 既有的
`jobs` 陣列）與 `cortex inspect status`（`manager.workflow_status_entry()`，
job rows 來自 `registry.list_jobs()`）都把同一次快照已經讀到的完整列表傳進
來，這裡統一篩選成該 run 的 job rows——兩條路徑用同一套規則，對同一個 run
才會算出一致的『目前 attempt』判定，也不需要為此額外全檔讀取（`manager.py`
新增一次 `registry.list_jobs()` 呼叫，讓 `_workflow_execution_identity()`
與新增的 job 事實判定共用同一份結果，不各自重讀）。

新增／改寫 `tests/test_decision_status_projection_840.py` 與
`tests/test_monitor_work_api.py` 共 12 個測試：以 live 形狀重現（step
executor/model 為 `None`、job 在飛、pointer 指向 n0 admit）驗證目前 attempt
可見；job 終局且卡未 passed、或這個 run/card 完全查無 job 證據，驗證
`decision-attempt-ended`；retry-card 後新 attempt 尚未建立 job 前驗證
superseded；新 attempt 的 job 真的建立且在飛後驗證重新可見；`:gN` 世代
attempt 正確對應到同一個 job；`cortex inspect status` 與 `cortex work show`
兩條路徑對同一狀態算出一致結果。既有依賴舊身分判定（步卡 executor/model
對齊 selected）才通過的 fixture（consistency-840、daemon-840、bytes-840、
e2e-840、e2e-work-show-840、retry-card-840 等）改成 live 形狀：步卡身分維持
`None`，改用真正的 registry job 佐證『目前 attempt』。

不改派工行為，不改 #839 決策邏輯（`_quota_admission_attempt_id`／
`reserve_for_candidate_with_generation_fallback`／
`_quota_admission_job_lookup_by_decision` 皆未變動）。

RED→GREEN 驗證：暫時把 `decision_projection.py`／`providers.py`／
`manager.py` 還原成 `git show HEAD:<path>`，新測試 11 項失敗（含既有
`test_monitor_work_api.py` 一項），其餘 28 項既有測試不受影響仍通過；復原
後 `tests/test_decision_status_projection_840.py`（39）、
`tests/test_monitor_work_api.py`、`tests/test_quota_admission_839.py` 共 90
項全數通過；`-k "status or snapshot or inspect or work_show or monitor or
work_api or quota"` 1026 passed／3 skipped。

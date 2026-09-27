# #839 額度感知准入與安全 attempt 邊界 fallback

新增 `paulsha_cortex/coordinator/quota_admission.py`：在既有候選分層排序、
runtime preflight、execution-profile 硬濾（pin／權限／角色／reviewer
independence／#842 qualification）之後，疊上一層額度感知准入——以 #836
`QuotaShadowService.project()` 的唯讀投影評估每個候選綁定的 pool/window 是
`sufficient`／`insufficient`／`unknown`（短窗夠、週窗不足一樣視為不可行；
找不到任何 pool 綁定的 profile 視為不受額度管理，維持既有派工行為），只替
**目前選中**的候選原子預留（消費 #838 `QuotaReservationAuthority`，不重建
其狀態機），成功才建立 job／spawn；race 落敗或額度不可行時重算下一個候選，
不先替候選池全體扣額度、不留半張 grant。

`decision_id_for()` 由 `(run_id, card_id, attempt_id, profile_key)` 決定性
算出，同一個 attempt 重送（restart／resume 補送）冪等回放、不二次扣款；真正
的新 attempt（安全 attempt 邊界 fallback 之後）才會有新的 `attempt_id`。
`AdmissionDecisionStore`（append-only、Trust Root 已登記）耐久保存每個
decision 綁定的 `run_id`／`card_id`／`attempt_id`／`profile_key`、
qualification／observation／demand／policy 版本、被排除候選與理由；
`reconcile_bound_reservations()` 提供 restart／crash 後的收斂掃描——job
registry 查不到時一律 `inconclusive`（不因為查不到就假設已終止並釋放
額度），只有確認終局才釋放容量。#837 operational usage forecast 尚未
落地，demand 只用明確版本化的 fixture（`DEMAND_FIXTURE_VERSION`），
receipt 上的 `demand_version` 可精確分辨 fixture 與真預測。

`manager._dispatch_workflow_card` 新增 `quota_admission_context` 參數
（`dispatch_workflow_card`／`resume_workflow_run` 亦同步透傳），缺省 `None`
時完全 no-op，逐字維持既有派工行為。給定 context 時：

- shadow（預設，`PSC_QUOTA_ADMISSION_ENFORCE` 未開）只記一份 decision
  receipt（連帶寫入 `WorkflowRun.quota_admission[persona]` 診斷投影），
  不呼叫 `reserve()`、不改變既有派工結果。
- opt-in enforce 時，額度不可行的候選會被排除、換下一個既有排序候選重試
  （重用 `_runtime_preflight_gate`／`_select_workflow_identity`，不重建
  候選池／不繞過既有 preflight／pin／independence 檢查），全部不可行才回
  精確 `quota-admission-insufficient` 等待理由，零 job、零假 job_id。
- reservation 生命週期嚴格對應 #838 現行協定：`reserve()` → `create_job()`
  → `bind(job_id)` → 才 spawn；`create_job()` 失敗（bind 之前）用
  `release(reason="fail-before-spawn")`；spawn 之後失敗（含派工時 429／
  infra 錯誤）只能 `settle(outcome="failed")`，記消耗但不對品質下判斷。

rollback 只需把環境變數改回非 `on`（或整條不傳 `quota_admission_context`），
不需要刪除已寫下的 decision receipt／reservation／consumption 證據。

新增 Trust Root 資產 `quota-admission-decisions`
（`config/paths.py:quota_admission_decisions_root()`，Manager-owned、
Monitor 唯讀）；`WorkflowRun` 新增 provenance-only 欄位 `quota_admission`
（比照既有 `model_qualification` 加法模式，`monitor/providers.py` 的
`_WORKFLOW_V2_OPTIONAL_ROW_KEYS` 封閉白名單同步更新，維持跨版本相容）。

新增 21 個測試：`tests/test_quota_admission_839.py`（decision 引擎單元測試，
含兩 process barrier 真實競爭）與 `tests/test_quota_admission_dispatch_wiring_839.py`
（透過 `dispatch_workflow_card` 的端到端接線：baseline 無 context 不受影響、
shadow 不擋派、獨立池合格替代可選同池替代拒絕、全候選不可行零 job、
fake executor 派前可用／spawn 時 429 記消耗釋回容量、兩 instance race 落敗
不建 job）。

本票刻意不重做候選排序／runtime preflight／#825 退避／#826 失敗分類／#830
非 Job producer 契約／#497 generation；`autonomy.py` 的 slice fanout（非
Job 消費端）不在本票接線範圍，維持既有行為。真正的 #836 provider 觀測來源
與 `manager_daemon.py` 的 `DispatchContext` 建構屬部署／安裝層，是獨立的
canary gate，本票只交付 Cortex 消費端。

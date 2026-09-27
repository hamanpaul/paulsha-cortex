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

- **#839 對抗審查修復（production 接線輪）**：`manager_daemon.py` 從未建構
  或傳入 `quota_admission_context`，production 完全不可達——`quota_admission`
  模組與 `manager._dispatch_workflow_card` 的接線只有測試在餵，daemon 五個
  dispatch／resume 呼叫點（workflow start、operator resume、periodic resume）
  逐字未動。新增 `paths.quota_pools_config_path()`（`config_root()` 底下，
  比照既有 `paulshaclaw.yaml` 豁免，支援 `PSC_QUOTA_POOLS_CONFIG` 覆寫）與
  `quota_admission.load_quota_pools_config()`／`parse_quota_pools_config()`
  （schema `cortex/quota-pools/v1`，逐字複用 #836
  `parse_pool_descriptor`／`parse_unit_definition`／`parse_binding`，不自寫
  第二套驗證）；`manager_daemon.py` 以此建構 `DispatchContext`（file-backed
  `QuotaEventLedger`／`QuotaReservationAuthority`／`AdmissionDecisionStore`，
  皆用預設路徑，以設定檔內容 sha256 digest 快取解析結果），並在五個呼叫點
  全部傳入；periodic tick 另呼叫新增的
  `manager.reconcile_quota_admission_reservations()`，同時收斂 `reserved`
  （見下方 MAJOR）與 `bound` 兩種狀態，job 進終局時若 binding 可解析同時
  呼叫 `QuotaShadowService.record_terminal_usage()` 記消耗。設定檔存在但
  無效時，shadow 降級記錄錯誤並回 `None`（維持不擋派工）；opt-in enforce
  下改回新增的 `quota_admission.QuotaConfigInvalid` 訊號物件，
  `_dispatch_workflow_card` 在建立任何 job 之前 fail closed，回精確等待
  理由 `quota-config-invalid`。
- 同輪另修三個 MAJOR：(1) `reserve_for_candidate()` 對排名第一的可行候選
  race 落敗（denied）時，原實作直接回 `quota-admission-insufficient`，完全
  跳過既有排序中其餘候選——原子預留改成在候選迴圈內、選中候選的當下立刻
  嘗試，race 落敗時排除該候選、依既有排序對下一個候選重試（有界，每次只
  替所選候選預留，不留半張 grant）。(2) `create_job()` 耐久寫入後、
  `bind()` 之前 crash 會留下無 job_id 的 `reserved` reservation，舊
  `reconcile_bound_reservations()` 只收斂 `bound`——新增
  `reconcile_reserved_reservations()`，依 `attempt_id` 的 ordinal 反查
  registry 找出對應建立的 job（找到且已終局 → `reconcile(confirmed-terminated)`；
  找到但非終局 → `reconcile(confirmed-alive)` 續 lease；查無此 job 且 lease
  已過期 → `reconcile(confirmed-terminated)`，evidence 標 `recovered-unbound`；
  查無此 job 但 lease 未過期，或查詢本身失敗 → 一律不動，不得誤放）。
  (3) `AdmissionDecisionStore.record()` 只 fsync 檔案本身，未 fsync 目錄——
  比照 #838 `_open_for_append` 每次 append 都 fsync 父目錄與其上層。
- Trust Root：`quota_pools_config_path()` 比照 `config_root()` 既有豁免，
  登記在 `ACKNOWLEDGED_NON_ASSET_PATHS`，不是 Manager-owned durable-state
  資產（operator-owned、Manager 唯讀消費，慣例與 `paulshaclaw.yaml` 相同）。
- 新增 21 個測試：`tests/test_quota_admission_839.py`（fsync 目錄、
  `reconcile_reserved_reservations` 五個分支）、
  `tests/test_quota_admission_dispatch_wiring_839.py`（race 落敗換獨立池
  候選）、`tests/test_quota_admission_daemon_wiring_839.py`（新檔：五個
  daemon 呼叫點接線、設定檔載入三態、periodic tick 收斂掃描接線、兩個端到
  端案例）。

- **#839 對抗審查修復（production 接線輪之後，第二輪三個 finding）**：
  1. **MAJOR（manager.py:13848）**：`reserve_for_candidate()` 的冪等回放
     （`status == "duplicate"`，代表這個 decision_id 的 reservation 在這次
     呼叫**之前**就已經存在——可能是另一個 Manager instance 剛贏得的
     grant）舊實作跟 `granted` 同等對待，若這個 instance 隨後在
     `create_job()`／provisioning 失敗，會用這個借來的 owner_token 呼叫
     `release()`，誤釋放另一個仍在使用中的 instance 的 grant。#838
     `reserve()` 本身無法分辨『這是我方稍早留下的紀錄』還是『別人剛贏的
     grant』（兩者回傳形狀逐字相同），因此改在 #839 側只認 `status`：只有
     這次呼叫自己拿到 `granted` 才建立 `quota_reservation_handle`（可信
     擁有者，後面才安全 release／bind）；`duplicate` 一律視為『別人持有』
     ——不建 job、不呼叫 release()／bind()，排除該候選換下一個既有排序
     候選重試，精確等待理由 `quota-admission-attempt-held-elsewhere`
     （#830 非 Job 決策契約）；這個 attempt 底下卡住的舊 reservation 是否
     已死，交給既有 `reconcile_reserved_reservations()`／
     `reconcile_bound_reservations()` 依 lease／job registry 事實判定。
     以直接呼叫 authority 模擬第二個 Manager instance（比照既有
     AC3／race-fallback 兩個測試的既有寫法：同一份 reservation authority
     檔案、先替 dispatch 即將算出的同一個 decision_id reserve）＋
     monkeypatch `registry.create_job` 一定失敗重現：RED 下該既有
     reservation 被誤 release 成 `released`；GREEN 下維持 `reserved`、
     sequence 不動、`registry.create_job` 從未被呼叫。
  2. **BLOCKER（manager.py:14379）**：spawn 時 429／infra 失敗的即時
     settle 路徑只呼叫 `settle(outcome="failed")`，沒有像 reconcile 路徑
     （`on_settled`）一樣呼叫 `QuotaShadowService.record_terminal_usage`
     記終局 usage——只有 restart 後的 periodic reconcile 掃描才補記，即時
     路徑漏記。修法：`_quota_admission_record_terminal_usage` 改吃
     `profile_key` 而非整個 `decision`（兩個呼叫端形狀不同——即時路徑只有
     `profile_binding.resolved_key`，沒有完整 `AdmissionDecision`），
     `reconcile_bound_reservations(on_settled=...)` 與 spawn 失敗的即時
     settle 路徑改共用同一支 helper；settle 成功（`ok`／`duplicate`）才記
     usage，settle 結構性被拒不記（交給既有 reconcile 依事實判定）。以
     spy 包一層 `QuotaShadowService.record_terminal_usage` 驗證：RED 下
     spawn 429 後從未被呼叫；GREEN 下恰好呼叫一次、`profile_key`／
     `job["id"]` 對得上。另核對三件既有事：reservation ledger 確實
     `settled(failed)` 並釋回容量（既有測試已覆蓋）、job 仍如實記
     `status=failed` 且 `provider_outcome.outcome != "quota"`（#826 分類不
     受影響，既有行為）；`record_executor_backoff_from_job` 只認
     `RATE_LIMITED`／`QUOTA` outcome，這條 launch-time 例外路徑
     （`classify_launch_failure`）恆分類為 `LAUNCH_FAILED`／
     `EXECUTABLE_NOT_FOUND`，本來就不會寫入 #825 backoff——這是 #826
     既有分類語意（launch() 直接丟例外時還沒有任何 provider 輸出可判斷
     429／quota），不在本票／#826 範圍內變更；因此「下一次 dispatch 在
     backoff 期間不選同一候選」對這條路徑不適用（backoff 從未寫入），未
     另補測試。
  3. **MAJOR（manager.py:11413）**：enforce 下全部候選被拒時
     `_quota_admission_stop()` 只標 `needs_human`，沒寫
     `AdmissionDecisionStore` receipt、也沒更新
     `WorkflowRun.quota_admission`；`_quota_admission_config_invalid_stop()`
     （設定無效路徑）同樣沒寫。新增共用 helper
     `_quota_admission_record_wait_decision()`：兩條路徑都留一筆
     `outcome="wait"` 的 receipt（沿用 `workflow.QUOTA_ADMISSION_OUTCOMES`
     既有封閉列舉 `{"admit", "wait"}`，不新增第三種 outcome 值——`wait`
     語意上已經精確覆蓋『目前沒有可派工的候選』，避免對 #840 receipt 形狀
     引入未宣告的新狀態），含被排除候選與各自理由（`excluded`）；
     decision_id 用固定 sentinel profile_key（`quota-admission:no-admissible-candidate`）
     搭配 `attempt_id` 算出，receipt 冪等（`store.get()` 先查已有紀錄就
     沿用，不因重送時觀測數字變了就試圖覆寫出矛盾內容）；store 讀寫任何
     失敗（含罕見併發衝突）一律靜默降級為 `None`，只影響這筆診斷投影，
     絕不讓已經確定的 fail-closed 派工結果變成派工。
     `_quota_admission_config_invalid_stop()` 這條路徑用預設路徑新建
     `AdmissionDecisionStore()`（該路徑收到的是 `QuotaConfigInvalid` 訊號
     物件，不是 `DispatchContext`，沒有 `.store`；decision receipt 落點
     `quota_admission_decisions_root()` 本身獨立於壞掉的 quota-pools
     設定檔，可安全建構）。兩條路徑都在同一次
     `registry._manager_update_workflow_run()` 呼叫內一併更新
     `facets`／`needs_human_reason`／`quota_admission[persona]`。以
     git-show 還原 production 檔重跑新測試確認 RED（receipt 不存在／
     `updated_run.quota_admission` 為 `None`），修復後 GREEN。
  - 未動候選排序／runtime preflight／pin／independence／Trust Root／品質
    規則；`WorkflowRun.quota_admission[persona]` 既有欄位形狀
    （`decision_id`／`mode`／`outcome`）與 `monitor/providers.py` 白名單
    皆未變動，`workflow._validate_quota_admission()` 既有封閉鍵集合
    （`{"decision_id", "mode", "outcome"}`）與既有 `QUOTA_ADMISSION_OUTCOMES`
    列舉沿用不擴充——receipt 形狀對 #840（status 投影）只做加法，不改
    既有欄位名稱或意義。
  - 新增 3 個測試（`tests/test_quota_admission_dispatch_wiring_839.py`）：
    `test_duplicate_reservation_from_concurrent_manager_is_never_released_on_failure`
    （MAJOR 1）；`test_spawn_time_429_after_bind_settles_failed_and_frees_capacity`
    擴充終局 usage spy 斷言（BLOCKER 2）；
    `test_opt_in_all_candidates_infeasible_returns_zero_job_with_precise_reason`
    擴充 receipt／quota_admission 投影斷言（MAJOR 3）。三者皆先以
    `git show HEAD:paulsha_cortex/coordinator/manager.py` 暫還原
    production 檔確認 RED，復原修法後轉 GREEN。

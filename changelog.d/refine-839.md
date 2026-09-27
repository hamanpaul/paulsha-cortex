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

- **#839 對抗審查修復（第三輪三個 MAJOR）**：
  1. **MAJOR（quota_admission.py:1008，`reconcile_reserved_reservations`）**：
     合法持有者在 `reserve()` 成功後、寫入 admit receipt 前 crash（或
     `AdmissionDecisionStore.record()` 本身失敗）會留下一筆從未被任何
     receipt 記錄過的 `reserved` reservation——舊實作以
     `AdmissionDecisionStore.enforced_admitted()` 反查候選再查
     `authority.status()`，這筆 reservation 對它完全隱形；
     `_committed_totals()` 本身不因 lease 過期自動放掉容量（過期永不證明
     可釋放，是既有設計，非本次修復對象），之後同一個 attempt 重送只會
     一直撞到這筆『看不見也放不掉』的 reservation。修法：新增
     `QuotaReservationAuthority.list_by_state(state, *, now_ms)`（#838
     唯讀新增，不改狀態機／寫入協定），收斂掃描改以此 API 為出發點——
     `run_id`／`card_id`／`attempt_id`／`decision_id` 本來就是 reservation
     記錄自己的欄位，不必經過任何下游 receipt 是否成功寫入；找到對應 job
     則依終局／存活收斂或續 lease，查無對應 job 且 lease 已過期則
     `reconcile(confirmed-terminated)` 釋放（evidence 標
     `recovered-unbound`），查詢失敗或 lease 未過期一律不動。以直接呼叫
     `reserve_for_candidate()`（**不**呼叫 `store.record()`）模擬這個 crash
     窗口重現：RED 下（`git show HEAD` 還原 `quota_admission.py`／
     `quota_reservation.py`）用空 store 呼叫舊版
     `reconcile_reserved_reservations()` 回傳空 tuple、reservation 永遠停
     在 `reserved`；GREEN 下用新版（不再需要 `store` 參數）直接掃到並在
     lease 過期後正確釋放。`reconcile_reserved_reservations()` 因此拿掉了
     `store` 參數（僅供列舉用，內部已不需要）；`manager.reconcile_quota_admission_reservations()`
     同步移除呼叫時傳入的 `store=`。
  2. **MAJOR（manager.py:13961，dispatch 候選迴圈 `duplicate` 分支）**：
     dispatch 在建出 job 前 reservation 已被 release（`create_job()` 失敗
     觸發 `release_reservation_before_spawn`）或經 reconcile 終結後，job
     從未真正建立過，`_quota_admission_attempt_id()` 依 job 數推算的
     `attempt_id` 完全不會前進——下一次 retry 用同一個
     `attempt_id`／`decision_id` 重新 `reserve()`，只會拿到同一筆已終結
     reservation 的 `duplicate` 回放；第二輪修法把所有 `duplicate` 一律當
     `held-elsewhere`，讓這種情況下的卡永久卡住、即使容量其實完全空著。
     修法：新增 `quota_admission.generation_attempt_id()` 與
     `reserve_for_candidate_with_generation_fallback()`——把『世代』疊在
     attempt_id 之上（世代 0 就是原始 attempt_id，向後相容；世代 ≥1 附加
     `:g{generation}`），`duplicate` 時檢查那筆舊 reservation 目前狀態：
     仍 `reserved`／`bound`（非本地持有）→ 原樣回傳，交給既有
     held-elsewhere 判斷；已 `released`／`settled`（終局）→ 決定性地換算
     下一個世代重新 `reserve()`，直到拿到非「duplicate-且終局」的結果或
     世代探測上限（重用既有 `_MAX_ATTEMPTS_PER_DECISION=64`）。同一次呼叫
     只在探測到的第一個尚未終結的世代真正呼叫會改變狀態的 `reserve()`
     （更早世代只讀到 duplicate、不寫入任何事件），確保重啟／重送同一次
     retry 仍冪等，不會一次 retry 產生兩個 grant。連帶修正
     `_quota_admission_job_lookup_by_attempt()`：世代後綴只影響
     reservation／decision 身分，job 是否已建立的判定仍看與世代無關的
     job-count ordinal，因此先剝掉 `:g{generation}` 尾綴才解析，否則世代
     ≥1 的 attempt_id 會因為 `isdigit()` 檢查失敗而永遠查不到其實已經
     建立的 job。以直接呼叫 authority 模擬「上一次 retry 已
     `release_reservation_before_spawn`」重現：RED 下（`git show HEAD`
     還原 `manager.py`）唯一候選撞到 `duplicate` 一律排除，回
     `quota-admission-insufficient`、零 job；GREEN 下改用下一個世代成功
     `granted`／建立 job／`bind()`，且上一個世代的 reservation 維持它原本
     的 `released` 狀態，沒有被誤動。
  3. **MAJOR（manager.py:14056，admit receipt 寫入）**：shadow（以及未受
     額度管理、`quota_reservation_handle` 為 `None`）的候選走到 admit
     receipt 這一步時，舊實作沒有先讀舊值就直接無條件
     `quota_admission_context.store.record(quota_decision)`——`reserve()`
     對同一個 decision_id 的重送本來就冪等，但
     `AdmissionDecision.generated_at_ms` 是每次呼叫當下的時間戳，retry
     時必然與上一次不同；`AdmissionDecisionStore.record()` 對同
     decision_id、內容不同（哪怕只差這個時間戳）一律視為衝突並拋
     `AdmissionDecisionCorrupt`，沒有任何 try/except 包住，讓這筆純診斷
     用途的寫入直接炸掉整條 `_dispatch_workflow_card`——即使是 shadow
     模式（票面契約：只記錄、絕不改變既有派工結果）也會被拖累，讓 shadow
     rollout 本身改變既有重試行為。修法：新增
     `_quota_admission_record_admit_decision()`，比照既有
     `_quota_admission_record_wait_decision()` 先 `store.get()`、有紀錄
     就直接沿用（回既有紀錄的 `mode`／`outcome`，不重寫），沒有才
     `store.record()`；store 讀寫任何錯誤（含衝突、IO、罕見損毀）一律
     靜默降級為 `None`，只影響這筆診斷投影／`WorkflowRun.quota_admission`
     投影——reservation（如果有）的存續完全交給
     `reconcile_reserved_reservations()`／`reconcile_bound_reservations()`
     依 authority／registry 事實判定，不依賴這筆 receipt 是否寫成功（見
     上面第 1 點的修法），因此對 enforced 模式也一律 fail soft 是安全的。
     以 monkeypatch 讓 `AdmissionDecisionStore.record` 恆定丟例外重現：
     RED 下（`git show HEAD` 還原 `manager.py`）shadow dispatch 直接讓
     `RuntimeError` 往外傳、整條 dispatch 中斷；GREEN 下派工結果（是否
     派工、選中的 executor、job 欄位、`WorkflowRun.quota_admission`）與
     完全沒有 `quota_admission_context` 的 baseline dispatch 逐字相同
     （`WorkflowRun.quota_admission` 兩者皆維持 `None`，不是半吊子的
     診斷投影）。
  - 未動候選排序／preflight／pin／independence／Trust Root／品質規則；
    receipt 欄位（`AdmissionDecision`／`WorkflowRun.quota_admission`）沿用
    既有形狀，只加了兩個純唯讀函式（`quota_admission.generation_attempt_id`／
    `reserve_for_candidate_with_generation_fallback`）與 #838 authority 的
    一個唯讀方法（`list_by_state`），沒有新增或修改任何 receipt／事件欄位。
  - 新增測試：`tests/test_quota_reservation_838.py`
    （`test_list_by_state_finds_reserved_reservation_even_without_any_downstream_receipt`／
    `test_list_by_state_partitions_reservations_by_current_logical_state`／
    `test_list_by_state_rejects_unknown_state`）；
    `tests/test_quota_admission_839.py`
    （`test_reserved_reservation_without_any_decision_receipt_is_still_reconciled`，
    並移除既有 5 個 `reconcile_reserved_reservations` 呼叫的 `store=`
    參數以配合新簽章）；`tests/test_quota_admission_dispatch_wiring_839.py`
    （`test_duplicate_reservation_already_terminated_advances_to_next_generation_and_dispatches`／
    `test_shadow_mode_store_record_failure_never_changes_dispatch_result`）。
    三個 MAJOR 皆先以 `git show HEAD:<path>` 暫還原對應 production 檔重跑
    新測試確認 RED，復原修法後轉 GREEN。

- **#839 對抗審查修復（第四輪三個 MAJOR）**：
  1. **MAJOR（manager.py:11491，admit receipt 寫入與 bound 收斂）**：同一個
     attempt／profile 先以 shadow 模式寫過一筆 admit receipt，operator 隨後
     才把 `PSC_QUOTA_ADMISSION_ENFORCE` 開成 `on` 重試同一張卡（job 尚未
     真正建立，attempt ordinal 不變）——舊版 `decision_id_for()` 不納入
     mode，兩次呼叫算出**同一個** decision_id；enforce 那次真正
     reserve／bind 出的 reservation 想寫 admit receipt 時，
     `_quota_admission_record_admit_decision`（第三輪修法：先讀舊值、有
     就沿用）會直接沿用 shadow 那筆舊記錄，enforced receipt 從未落地，
     `WorkflowRun.quota_admission` 永遠卡在 shadow；`reconcile_bound_reservations`
     的舊實作又以 `store.enforced_admitted()`（過濾 `mode=="enforced"`）
     反查候選，這筆真正 bound 的 reservation 因此對它完全隱形，job 終局
     後容量卡死。修法二合一：(a) `decision_id_for()` 新增必填 `mode`
     參數（`"shadow"`／`"enforced"`）納入雜湊輸入，讓兩種模式的決策身分
     隔離、可並存各自冪等（`reserve_for_candidate_with_generation_fallback`
     內部固定用 `mode="enforced"`，因為它只在 enforce 路徑被呼叫）；
     (b) `reconcile_bound_reservations` 改以
     `authority.list_by_state("bound", ...)` 為出發點（比照
     `reconcile_reserved_reservations` 既有做法），`store` 只在
     `on_settled` 需要診斷用 `profile_key` 時才被動查詢
     （`store.get(status.decision_id)`），查不到（receipt 從未寫入或
     mode 隔離之前的舊碰撞）不影響容量收斂本身，`on_settled` 收到 `None`
     即安全略過 usage 記錄。`WorkflowRun.quota_admission[persona]` 的
     `{decision_id, mode, outcome}` 三鍵閉包形狀（`workflow._validate_quota_admission`）
     不變——`decision_id` 改變只是它指向的字串值變了，不是形狀變了；
     `AdmissionDecision` 也未新增欄位。**#840 定位方式**：projection 本身
     已經內嵌當前 `decision_id`（每次成功 dispatch 都覆寫），#840 讀
     `run.quota_admission[persona]["decision_id"]` 拿到的永遠是最新一次
     的身分，不需要另外枚舉 store 猜測哪筆是「現在的」。以直接 dispatch
     兩次（第一次 shadow、monkeypatch `registry.create_job` 使其在第一次
     呼叫失敗以保持 ordinal 不動，第二次開 enforce）重現：驗證兩個
     decision_id 不同、shadow 那筆完全沒被動過、`WorkflowRun.quota_admission`
     更新成 enforced，且這筆 bound reservation 能被
     `reconcile_bound_reservations` 正確收斂。
  2. **MAJOR（manager.py:11603，reserved sweep 反查 job）**：舊實作
     `_quota_admission_job_lookup_by_attempt` 純依
     `_quota_admission_attempt_id` 的 ordinal（`f"{run_id}:{card_id}:n{prior}"`）
     反查 registry——只反映「這個 run/card 目前有幾個 job」，不指向任何
     特定候選／decision。Manager A 對候選 A 的某個 attempt `reserve()` 後
     crash（`create_job()` 從未發生）；Manager B（另一個 instance，或稍後
     retry）改派候選 B（不同 pool／profile）在同一個 ordinal 建出它自己的
     job 時，舊實作會把 B 的 job 誤當成 A 的存活證據，誤 renew／誤判 A 的
     reservation 存活，即使兩者完全無關。修法：`registry.create_job()`
     新增選填欄位 `quota_decision_id`（純加法，預設 `None`；`_validate_loaded_job`
     的既有字串型別檢查清單同步新增這個欄位，registry 對 job dict 本來就
     沒有封閉 key-set 檢查——核對 `d99df4d9` 與現版 `_validate_loaded_job`
     逐字相同，確認舊版 Manager／Monitor 讀到多一個未知鍵不會 fail closed；
     只有真的取得 reservation 的候選才寫入這個欄位，shadow／未受額度管理
     的候選維持 `None`，與完全沒接線時的 job 記錄逐字相同，不引入 shadow
     可觀察的新副作用）。`_quota_admission_job_lookup_by_attempt` 改名為
     `_quota_admission_job_lookup_by_decision`，改依 job 建立時記錄的
     `quota_decision_id` 精確比對；`quota_admission.reconcile_reserved_reservations`
     的 callback 參數同步改名為 `job_lookup_by_decision`，改傳
     `status.decision_id`（原本傳 `status.attempt_id`）。以直接建構兩個
     不同 decision_id、只替其中一個建出 job（同一個 run/card，ordinal 0）
     重現：舊版 `_quota_admission_job_lookup_by_attempt` 對另一個
     decision_id 反查也會誤命中這個不相關的 job（獨立腳本重現，非測試
     套件常駐項）；新版精確比對，A 查無、B 查有；另補「job 缺
     `quota_decision_id`（舊版 job）→ 一律視為查無」的相容測試。
  3. **MAJOR（quota_admission.py:1119，reserved lease 過期即回收 vs.
     provisioning 尚未結束）**：`reconcile_reserved_reservations` 對「lease
     過期且查無對應 job」直接視為可回收，但 reserve() 之後到
     provisioning（worktree／sandbox 建立）完成、`create_job()`／`bind()`
     之前還有一段耗時窗口；lease 設太短或 provisioning 恰好變慢時，會釋放
     一筆原 dispatch 仍在使用中的 reservation，讓另一個 Manager 重拿同額度、
     原 dispatch 稍後 `bind()` 時撞上衝突。修法四件套：(a) `QuotaReservationAuthority`
     新增 `renew()`（#838 純加法新事件種類，只允許從 `reserved` 續租，
     與 `reconcile(confirmed-alive)` 的續租分支服務不同情境——後者刻意不
     驗證 owner_token，給 crash 後第三方復原用；`renew()` 給仍活著、知道
     自己 owner_token 的原 dispatch 用，兩者不重疊）；`_dispatch_workflow_card`
     拿到 grant 之後立刻續租一整個新的 `lease_ms` 窗口。(b) 新增
     process-global `quota_admission.IN_FLIGHT_DISPATCHES`
     （`InFlightDispatchTracker`，純記憶體、不耐久）：dispatch 拿到 grant
     後 `mark_started`，`create_job()`／`bind()` 的 try/except 外包一層
     `finally` 保證 `mark_finished`（成功／結構性失敗／任何例外皆會清除，
     不會永久卡住）；`reconcile_reserved_reservations` 新增選填 `in_flight`
     參數，命中時即使 lease 早已過期也續租而不釋放。(c) `reconcile_reserved_reservations`
     新增選填 `grace_ms`（預設 `0`，逐字沿用舊行為；daemon 端傳入一整個
     `lease_ms`）——次要防線，主要防線是 (a)(b)。(d) `bind()` 仍可能撞上
     `conflict`／`reservation-already-terminal`（例如 renew 呼叫本身失敗、
     clock skew）：job 記錄已建立，`_dispatch_workflow_card` 標記它失敗
     （**不**記 `provider_outcome`，避免誤觸 #825/#826 executor backoff
     分類——這不是 executor 的錯）、留一筆 `outcome=wait` 的 decision
     receipt 並更新 `WorkflowRun.quota_admission[persona]`，再讓例外照
     既有「job 已建立、spawn 前失敗」路徑傳播（fail closed，不 spawn、
     不假稱成功）。另在 `parse_quota_pools_config` 加 `lease_ms` 下限
     （1 分鐘）——低於下限拒絕載入，enforce 下回 `quota-config-invalid`；
     太短的 lease 會讓上面的安全網疲於奔命。以直接呼叫
     `reserve_for_candidate`（不 bind）模擬 crash 窗口＋設 `now_ms` 遠遠
     超過 lease＋grace 重現 RED（舊版釋放）／GREEN（`in_flight` 命中時續
     租，未命中時仍安全釋放）；`bind()` monkeypatch 回傳
     `reservation-already-terminal` 重現 job 標記失敗＋wait receipt 的
     fail-closed 路徑。
  - 未動候選排序／runtime preflight／pin／independence／Trust Root／品質
    規則。`decision_id_for()` 的雜湊輸入改變（新增必填 `mode`）改變了未來
    產生的 decision_id 字串值，但這是 #839 尚未經過 installed／live
    accepted 檢查點前的身分推導調整，不影響任何已耐久寫入事件的可讀性
    （舊事件本身不重新驗證雜湊來源）。
  - 新增測試：`tests/test_quota_reservation_838.py`
    （`test_renew_extends_lease_and_keeps_reserved_state`／
    `test_renew_rejects_bound_reservation`／
    `test_renew_rejects_terminal_reservation`／
    `test_renew_rejects_wrong_owner_attempt_or_sequence`／
    `test_renew_rejects_invalid_lease_ms`／
    `test_renew_is_durable_and_survives_reload`）；
    `tests/test_quota_admission_839.py`
    （`test_bound_reservation_without_any_decision_receipt_is_still_reconciled`／
    `test_on_settled_receives_none_decision_when_receipt_missing_and_usage_recording_is_skipped`／
    `test_bound_reservation_with_receipt_present_still_passes_decision_to_on_settled`／
    `test_reserved_unbound_lease_expired_but_within_grace_is_left_untouched`／
    `test_reserved_unbound_lease_expired_beyond_grace_is_released`／
    `test_reserved_unbound_in_flight_dispatch_renews_even_after_lease_expired`／
    `test_reserved_unbound_not_in_flight_of_this_process_is_released_normally`／
    `test_in_flight_dispatch_tracker_mark_finished_removes_entry`／
    `test_quota_pools_config_rejects_lease_ms_below_floor`／
    `test_quota_pools_config_accepts_lease_ms_at_floor`）；
    `tests/test_quota_admission_dispatch_wiring_839.py`
    （`test_switching_shadow_to_enforce_mid_attempt_writes_distinct_enforced_receipt`／
    `test_reserved_sweep_job_lookup_does_not_misattribute_unrelated_candidates_job`／
    `test_reserved_sweep_ignores_legacy_job_missing_quota_decision_id_field`／
    `test_successful_dispatch_renews_lease_and_clears_in_flight_marker`／
    `test_bind_conflict_reservation_already_terminal_fails_closed_with_wait_receipt`）。
    三個 MAJOR 皆先以 `git show HEAD:<path>` 暫還原對應 production 檔（或
    直接呼叫舊函式名重現）重跑新測試確認 RED，復原修法後轉 GREEN；另在
    CPU 滿載（`nproc * 2` 個忙迴圈）下重跑四個既有測試檔 6 輪全數通過。

- **#839 對抗審查修復（第五輪兩個 bug）**：
  1. **MAJOR（manager.py:14098，mark_started 與 try/finally 不緊鄰）**：
     reserve 成功後，`quota_admission.IN_FLIGHT_DISPATCHES.mark_started()`
     立刻執行，但負責清除它的 `finally`（`mark_finished`）在原本的程式碼
     裡要到大約 340 行之後——`create_job()` 之前的整段
     worktree／sandbox provisioning（尤其 `creator.create()`，含 planner
     sandbox 複製與 reviewer sandbox 建立）與其他任何例外（例如
     `reviewer builder job unavailable`）都夾在中間、完全沒有 try 保護。
     這段期間任何一次失敗都會讓這個 reservation_id 永遠卡在 in-flight
     集合裡（periodic sweep 因此對它無限續租），同時這筆 reservation 本身
     也從未被 `release()`——下一次同 attempt 重送只會一直撞到這筆『看不見
     也放不掉』的 duplicate（held-elsewhere）。修法：把涵蓋 provisioning
     全程的 try/finally 往前挪，緊接在 `mark_started()` 之後就進入 try
     （連同 renew() 呼叫本身也納入保護），一路涵蓋 `_record_resolved_model_chain`、
     admit receipt 寫入、`reserved_job_id` 配發、worktree／sandbox
     provisioning、`create_job()`、`bind()`；`quota_job_created` 旗標的
     初始化同步往前移到同一個位置。既有的
     `except BaseException: if not quota_job_created and quota_reservation_handle
     is not None: release_reservation_before_spawn(...)` 與
     `finally: mark_finished(...)` 邏輯本身不用改——擴大 try 的涵蓋範圍後
     它們自然接住 provisioning 階段（含 `creator.create()`）的任何失敗，
     不需要另外補寫第二套清理路徑。以 monkeypatch worktree creator 使
     `create()` 在第一次呼叫時拋例外重現：RED 下（`git show HEAD` 還原
     `manager.py`）失敗後這筆 reservation 仍是 `reserved`（從未
     release）；GREEN 下確認 release 成功（`released`，
     `reason="fail-before-spawn"`）且 in-flight 集合已清空，接著同一組
     `run`／`card` 立刻重送第二次，`reserve_for_candidate_with_generation_fallback`
     自動換算下一個世代乾淨拿到新 grant 並成功派工，不撞
     `quota-admission-attempt-held-elsewhere`。
  2. **MAJOR（quota_admission.py:1270，`reconcile_reserved_reservations`
     的 in-flight 排除排在 job 反查之後）**：舊實作只在『查無對應 job』的
     分支才檢查 `in_flight`；但 `create_job()` 之後、`bind()` 之前這段
     窗口內 job 記錄已經存在（且必然未終局，見 #838 協定），job 反查一定
     命中，直接落到『job 找到但非終局』分支去
     `reconcile(confirmed-alive, renew_lease_ms=...)`——即使這筆
     reservation 明明是本 process 自己標記的 in-flight，也會被無條件續租、
     把 sequence 往前推一格。原 dispatch 手上 `quota_reservation_handle`
     快取的 `expected_sequence` 因此變成過期值，接下來呼叫的 `bind()`
     必然撞上 `conflict`／`sequence-mismatch`，job 記錄已建立但沒有走
     `reservation-already-terminal` 的既有 fail-closed 分支（那個分支只認
     那一個 reason），直接 raise ValueError 讓整條 dispatch 中斷、job 沒被
     標失敗，sweep 之後還會持續對它續租，容量因此永久卡住（本 process 自己
     的 sweep 跟自己的 dispatch 打架）。修法三件套：(a) 把 `in_flight` 檢查
     移到 job 反查**之前**、迴圈最頂端——命中就直接
     `skipped`（`detail="in-flight-dispatch-provisioning"`），不呼叫
     `job_lookup_by_decision`、不 `reconcile`、不 `renew`、不 `bind`、不
     `release`，把是否續租完全交還給正在進行中的 dispatch 自己（見下面
     manager.py 的 renew() 呼叫）。這修的是『同一個 process 自己的 sweep
     跟自己的 dispatch 互撞』；`in_flight`（process-local 記憶體）天生看
     不到**另一個** Manager instance 的 sweep，所以還需要 (b)：
     `manager._dispatch_workflow_card` 的 `bind()` 呼叫改成，若第一次回
     `conflict`／`sequence-mismatch` 且 `state` 仍是 `reserved`（代表
     owner_token／attempt_id 都對得上、reservation 沒被終結，只是 sequence
     被其他人合法推進過），就用回傳的最新 `sequence` 重新 `bind()` 一次
     （只重試一次，不是搶鎖迴圈）。(c) `bind()` 最終仍失敗時（含重試後仍
     衝突）：原本只有 `reservation-already-terminal` 那個特例會走
     job-標失敗＋wait-receipt 的 fail-closed 路徑，其餘衝突理由直接 raise
     裸例外、job 從未被標記——現在不分理由，`bind_result.status not in
     ("ok", "duplicate")` 一律走同一條既有 fail-closed 路徑（標記
     `status=failed`、不記 `provider_outcome`、留 `outcome=wait` 的
     decision receipt、更新 `WorkflowRun.quota_admission[persona]`），再讓
     `ValueError` 照既有『job 已建立、spawn 前失敗』路徑傳播。以直接呼叫
     `reconcile_reserved_reservations()`（模擬本 process 自己的 sweep 插在
     `create_job()` 與 `bind()` 之間）與直接呼叫 `authority.reconcile()`
     （模擬**另一個** instance 的 sweep，不經過 `in_flight`）兩種方式重現：
     RED 下（`git show HEAD` 還原 `quota_admission.py`／`manager.py`）
     前者觸發 `job-lookup-failed`／`reconciled` 而非 `skipped`，兩者最終都
     讓 dispatch 的 `bind()` 撞上未重試的 `sequence-mismatch` 直接
     ValueError 中斷；GREEN 下前者確認 outcome 為 `skipped` 且 sequence
     完全未被 sweep 寫入事件改變，後者確認 `bind()` 重試一次後乾淨成功、
     reservation 進入 `bound`。
  - 未動候選排序／runtime preflight／pin／independence／Trust Root／品質
    規則；receipt／job 欄位形狀不變，只調整既有 fail-closed 分支的觸發
    條件（原本只認一種 reason，現在對所有非 ok/duplicate 的 bind 結果一律
    適用同一條既有路徑），未新增任何欄位。
  - 新增／調整測試：`tests/test_quota_admission_839.py`
    （`test_reserved_unbound_in_flight_dispatch_renews_even_after_lease_expired`
    改寫——第四輪版本斷言 in-flight 時 sweep 會 `reconciled`／續租 lease，
    這其實正是本輪要修的同一種『sweep 跟自己打架』風險，改斷言
    `skipped`、job 反查從未被呼叫、sequence／lease 完全未被 sweep 改動）；
    `tests/test_quota_admission_dispatch_wiring_839.py`
    （`test_provisioning_failure_before_create_job_clears_in_flight_and_releases_for_clean_retry`／
    `test_reserved_sweep_skips_this_process_in_flight_reservation_before_job_lookup`／
    `test_concurrent_instance_sweep_renew_between_create_job_and_bind_retries_and_succeeds`）。
    兩個 MAJOR 皆先以 `git show HEAD:<path>` 暫還原對應 production 檔重跑
    新測試確認 RED，復原修法後轉 GREEN；另在 CPU 滿載
    （`nproc * 2` 個忙迴圈）下重跑
    `tests/test_quota_admission_839.py`／`test_quota_admission_dispatch_wiring_839.py`／
    `test_quota_admission_daemon_wiring_839.py`／`test_quota_reservation_838.py`
    共 6 輪全數通過。
  - **第三輪『renew 事件讓 038e0901 讀不懂』判定不成立的補充說明**：對抗
    審查第三輪曾提出一個 finding，主張新增 `renew` 事件種類會讓
    `038e0901` 這個版本的 reader 讀不懂既有 reservation 檔案；查證後這個
    判定不成立——`038e0901` 從未發布過，上一個真正發布的版本是
    `d99df4d9`，而 `d99df4d9` 完全沒有 reservation store（#838 是之後才
    落地的功能），沒有『舊版讀不懂新事件種類』這個相容性問題存在的前提；
    `#836`／`#838`／`#839`／`#840` 全部同在 `0.1.11` 這一次發布內落地，
    對外沒有任何中間版本曾經讀過缺少 `renew` 支援的 reservation 檔案格式。
    這裡不再另補程式碼修法，僅記錄判定結果。

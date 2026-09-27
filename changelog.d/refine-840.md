# #840 投影動態派工決策與額度等待來源（refine R10）

新增 `paulsha_cortex/monitor/decision_projection.py`：唯讀投影每個 run 目前
attempt 的 quota-aware admission（#839）決策摘要與額度等待來源。只消費既有
producer 的已寫證據——`AdmissionDecisionStore`（Manager-owned、Trust Root
登記為 Monitor 唯讀）與 `WorkflowRun.quota_admission`／`needs_human_reason`
（#527 `DiagnosticReason`）——不寫入 registry／workflow 檔案，不重算 #828 的
actual/planned/last identity 或 #830 的非 Job 決策契約，也不重做 #839 的准入
邏輯或 reservation 生命週期。

投影輸出每個 persona 的 `decision_id`、`mode`（`shadow`／`enforced`）、
`outcome`、`policy_version`／`policy_config_revision`、`observation_version`／
`demand_version`／`qualification_version`、`requested_profile_key`／
`resolved_profile_key`（取自 #835 `execution_profile_bindings` 的
`request_key`／`resolved_key`）、allowlist 過的 `selected`／`excluded`
候選身分（僅 `executor`／`model_id`／`independence_domain`／
`exclusion_reason`，不含 credential／env／raw prompt）、`reservation_id`，
以及 `classification`（`demand`：`confirmed`／`estimated`／`not-applicable`；
`observation`：`confirmed`／`unknown`／`not-applicable`）。合法的額度等待
（非 Job 決策，`quota-admission-insufficient`／`quota-config-invalid`）投影
成獨立的 `wait` 欄位，逐字帶出 #527 理由的 `reason`／`detail`／
`next_step_hint`／`context`，不自行編造 next_actions；沒有任何 #839 證據時
完全不出現在 entry 上，維持既有 attention 形狀不變。

新增 `DecisionReadCache`：decision store 暫時讀不到（損毀／IO 錯誤）時保留
上一次成功讀到的內容，並附精確的 `stale`／`stale_reason`／`stale_since_ms`
——不得把過期選模或 unknown remaining 呈現成『現在可派』。store 本身
append-only、單一 decision_id 內容不可變，因此 restart 後以全新 store／cache
handle 重讀能得到相同結果，不需要跨行程持久化這份快取。

`manager.workflow_status_entry()`（`cortex inspect status` 的 `attention`
清單）與 `monitor.providers.WorkflowRegistryProvider.scan()`
（`cortex work show` 的 observations 通道，新增 `quota_decisions` 觀測鍵，
比照既有 `needs_human_reasons`／`candidate_git_bases` 的可選欄位模式，不新增
WorkflowRun 欄位）共用同一份投影函式，並在同一次 snapshot 內共用同一個
decision store handle／`DecisionReadCache` 實例，確保兩個呈現面對同一個 run
算出一致的 decision／reason／freshness。`monitor.work_api.WorkReadModelStore`
與 `cli.py`／`porcelain/inspect.py` 的文字模式渲染同步補上 `quota_decision`
欄位的輸出。

**#839 receipt 最小加法**（本票發現、非重做既有邏輯）：`AdmissionDecision`
過去只留一個不可逆的 `observation_version` 指紋，事後看不出選中候選本身是
sufficient／unknown（shadow 模式下候選不可行一樣會被 admit）、也看不出這筆
決策當時用的是哪一版 operator quota-pools 設定檔。新增三個選填欄位（比照
#839 `QuotaPoolsConfig` 的可選欄位加法模式，缺席即 `None`，投影面視為
`unknown`，不臆測）：`selected_observation_state`／`selected_feasible`（對應
`CandidateAssessment.observation_state`／`.feasible`）與
`policy_config_revision`（對應新增的 `DispatchContext.config_revision`，由
`manager_daemon.py` 從 quota-pools 設定檔的 `config_revision` 帶入）。未改變
准入判定、reservation 生命週期或既有欄位語意。

新增 21 個測試：`tests/test_decision_status_projection_840.py`，覆蓋多
persona／retry／跨 run-card exact-key、negative（wait 無 Job、unknown
remaining、缺 provenance 的 legacy receipt、needs_human 語意保留、planned
不造 actual）、`workflow_status_entry` 與 `WorkflowRegistryProvider.scan()`
對同一份 snapshot 一致、restart／last-good stale、bytes 不變、allowlist
負面 fixture，以及以真實 `AdmissionDecisionStore` 寫入 receipt 到
`cortex inspect status`／`cortex work show` 文字輸出的端到端案例。

本票刻意不改派工行為、不改 #839 決策邏輯；status 熱路徑只讀既有 Trust Root
資產與 registry row，不評測、不探測 provider。installed／live canary 仍是
獨立部署 gate，未在本票執行。

## 對抗審查修復（5 條 MAJOR，本輪追加）

1. **`AdmissionDecision` 三個 #840 選填欄位不需要 schema bump**：
   `selected_observation_state`／`selected_feasible`／`policy_config_revision`
   與 #839 本身同一個 release 一起發布——#839 從未獨立上線過，不存在
   「#839-only（無 #840）的已發布版本讀不懂這三個新 key」的相容性缺口，
   `schema_version` 維持 `1`。三欄位是 v1 內的選填鍵加法（比照 #839
   `_QuotaPoolsConfig` 既有模式），讀取端（`_read_fd`）本來就已容忍缺這三
   個 key 的舊 row；補了 `tests/test_quota_admission_839.py` 的
   `test_store_get_tolerates_legacy_row_missing_840_optional_keys` 直接在
   store 層級鎖住這個容忍度（先前只間接由 `decision_projection` 的 legacy
   receipt 測試覆蓋）。
2. **`AdmissionDecisionStore.record()` 冪等重放誤判 fix**（`quota_admission.py`）：
   原本用磁碟上的 raw row 直接跟新算出的 `to_row()` dict 做相等比較——缺
   三個 #840 選填欄位的既有紀錄（#839-only 時期寫的）重送同一個
   `decision_id` 時，兩邊 key 集合不同，即使語意完全等價（都是
   unknown／None）也會被誤判成 `admission-decision-id-conflict`。改成
   `existing` 先經 `AdmissionDecision.from_row().to_row()` 正規化（缺席鍵
   補齊為 `None`）再比較，真正矛盾（正規化後仍不同）才 fail closed。補
   `test_record_replay_of_legacy_row_missing_840_optional_keys_is_idempotent_not_conflict`。
3. **`work_api.WorkReadModelStore._quota_decision()` 跨 repo 洩漏 fix**：
   原本只憑 provider_id 前綴是不是 `workflow:` 就挑第一個命中的
   `quota_decisions[work_id]`，完全沒比對 `repo` 參數；兩個 repo 若剛好有
   同一個 `work_id`（跨 repo 不保證唯一）會把另一個 repo 的
   quota_decision 錯配過來。改用 `_provider_repo()`（沿用 work_api 既有的
   provider↔repo 比對慣例）做 exact `(repo, work_id)` 匹配。補
   `test_quota_decision_is_scoped_to_exact_repo_not_leaked_across_repos`
   （`tests/test_monitor_work_api.py`）。
4. **`quota_decision_cache` 跨快照存活 fix**（`manager_daemon.py`）：
   `DecisionReadCache` 過去建在 `build_runtime_status_provider()` 的
   `provider()` closure 內部，每輪 status 快照都重建一個空 cache——上一輪
   成功讀到的 decision 在下一輪 store 暫時損毀／權限錯誤時完全遺失，退化成
   單純的『這次讀不到』而非 last-good。改成建在
   `build_runtime_status_provider()` 這層（呼叫一次、daemon 生命週期內共用
   的閉包變數），`provider()` 每輪呼叫沿用同一個實例。補
   `test_daemon_wired_cache_persists_across_snapshot_ticks_for_last_good_stale`：
   同一個 `provider` closure 呼叫兩次模擬兩輪 tick，第二輪損毀時驗證仍
   完整保留第一輪 last-good 內容（`mode`／`decision_id`）＋精確 stale
   原因。
5. **status 熱路徑每個 persona 都全檔重讀 fix**（`decision_projection.py`／
   `quota_admission.py`）：`DecisionReadCache.get()` 過去對每個 persona 都
   直接呼叫 `AdmissionDecisionStore.get()`（全檔線性掃描），即使 store／
   cache 已經在同一輪快照內共用，append-only 檔仍會被重複讀 N 次（N＝run
   數×persona 數）。新增 `AdmissionDecisionStore.all_rows()`（`get()`／
   `enforced_admitted()` 既有 `_read()` 的公開包裝，不新增驗證規則）與
   `DecisionReadCache` 內部的 `decision_id → row` 全檔索引，只在檔案身分
   `(size, mtime_ns, inode, mode)` 改變時才重讀（權限位刻意納入身分——純
   chmod 不動 size／mtime／inode，但 `_check_file` 的 fail-closed 判定正
   是靠權限位；不用 `ctime` 是因為部分檔案系統（含本機開發環境常見的
   WSL2）metadata-only 變更不保證更新 ctime 解析度）。補
   `test_shared_cache_reads_store_at_most_once_per_snapshot_across_many_runs_and_personas`：
   3 個 run×2 個 persona＝6 次查詢，監看 `all_rows()` 只被呼叫一次。

## 對抗審查修復（第二輪，2 條 MAJOR＋1 條判定不成立）

1. **`WorkModelRefresher.refresh()` 每輪重建 provider 導致 `DecisionReadCache`
   跨快照失效 fix**（`work_api.py`／`providers.py`）：上一輪（第一輪對抗審查）
   已經把 `manager_daemon.build_runtime_status_provider()`（`cortex inspect
   status` 路徑）的 `quota_decision_cache` 搬到 daemon 生命週期層級共用，但
   `cortex work show` 走的是另一條路徑——`WorkModelRefresher.refresh()` 每輪
   都經 `workflow_provider_factory(repo)` 重建一個全新的
   `WorkflowRegistryProvider` 實例，provider 內部的 `DecisionReadCache`
   （舊行為：建在 `__init__` 內部、呼叫端無法注入）因此每輪都是空的——第一輪
   成功讀到 decision，第二輪 `decisions.jsonl` 權限錯誤／損毀時，
   `cortex work show` 只剩 `available=false`／`stale_reason`，遺失第一輪的
   last-good `mode`／`selected`。
   修法：`WorkflowRegistryProvider.__init__` 新增可選的 `quota_decision_cache`
   注入參數（不注入時維持舊行為，自建一個，不影響其他呼叫端）；
   `WorkModelRefresher` 比照既有 `issue_sync_store`／`event_spool`（『必須
   活得比 per-repo provider 久』）的既有模式，在自己的 `__init__` 建立
   `_quota_decision_store`／`_quota_decision_cache`（daemon／refresher 生命
   週期內只建一次），`refresh()` 內使用**預設** provider factory 時逐輪把
   同一個 cache／store 實例注入新建的 provider；自訂
   `workflow_provider_factory`（測試／上層組裝）呼叫維持原樣（僅 `repo`
   單一參數），不受影響。補
   `test_refresher_quota_decision_cache_survives_across_refresh_ticks_for_last_good_stale`
   （`tests/test_monitor_work_api.py`）：refresher 兩輪 `refresh()`——可讀→
   （chmod 破壞群組權限位模擬）不可讀，驗證 `get_work_item()` 仍顯示
   last-good 的 `mode`／`selected` 並帶精確 `stale_reason`；同時在同一次
   損毀狀態下另建一個獨立的 `DecisionReadCache` 呼叫
   `manager.workflow_status_entry()`，斷言 `cortex inspect status` 與
   `cortex work show` 算出一致的 `decision_id`／`mode`／`selected`／
   `outcome`（兩個呈現面各自持有獨立 cache 實例，不共用同一個物件，模擬
   真實部署形狀）。
2. **wait receipt 無條件借用上一個 attempt 的 `execution_profile_bindings`
   fix**（`decision_projection.py`）：`_project_persona_decision()` 過去不論
   `decision.outcome` 一律用 `execution_profile_bindings[persona]` 覆寫
   `requested_profile_key`／`resolved_profile_key`。但 `execution_profile_bindings`
   是 #835 既有的 per-persona『最近一次真正派工成功』快照，只有真的走到
   `manager._record_resolved_model_chain()`（與 admit 決策同一次候選迴圈
   迭代內依序寫入）才會被覆寫——retry-card 之後，若這次 attempt 全數候選
   被拒（`quota-config-invalid`／全拒 wait，`outcome=="wait"`，
   `selected=None`），這次 attempt 從未走到那個寫入點，run 上的 binding
   **必然**是上一個仍 admit 的 attempt 留下的舊值。舊實作把這份舊 binding
   當成這次 wait 的結果疊上去，讓 status／work show 把上一輪的
   requested／resolved profile 誤呈現成當前 wait 的結果。
   修法：只有 `decision.outcome == "admit"` 且 `decision.selected is not
   None`（等價於這個 decision 確實選中了候選）時才疊加
   `execution_profile_bindings` 的 `request_key`／`resolved_key`；否則（wait
   receipt，沒有選中候選）`requested_profile_key` 維持 `None`、
   `resolved_profile_key` 明確改成字串 `"unknown"`（不再沿用
   `AdmissionDecision.profile_key` 內部 sentinel `quota-admission:
   no-admissible-candidate` 逐字外露，避免呼叫端誤把它當成一個真的 profile
   key 格式）；`excluded` 被排除候選清單不受本次修法影響，照舊帶出。補兩個
   測試（`tests/test_decision_status_projection_840.py`）：
   `test_wait_receipt_after_retry_does_not_borrow_previous_attempt_profile`
   （先 admit attempt n0、再 retry-card 出一筆 wait attempt n1，斷言
   requested／resolved 不再洩漏 n0 的 profile key，改為 `None`／`"unknown"`）
   與 `test_admit_receipt_still_uses_execution_profile_binding_for_same_attempt`
   （對照組：outcome=="admit" 時 binding 仍照舊疊加，確認本次修法只收斂
   wait，不影響既有 admit 語意）。
3. **`AdmissionDecision` 三個 #840 選填欄位 schema bump 疑慮（判定不成立，
   不改碼）**：對抗審查第二輪另提出「schema_version=1 下寫入
   `selected_observation_state`／`selected_feasible`／`policy_config_revision`
   三個選填鍵，回退到 `#839`-only 基準（commit `0cda9ec2`）讀不懂」的疑慮。
   查證：`#839` 與 `#840` 同一個 release 一起發布，`0cda9ec2`（`#839` 落地
   commit）從未獨立發布過、不存在任何『只有 #839、沒有 #840』的已發布版本
   需要相容；因此不存在真實的回退相容性缺口，`schema_version` 維持 `1`
   正確，不需要 bump，也不需要改碼。上方「對抗審查修復（5 條 MAJOR，本輪
   追加）」第 1 點已記錄過同一個結論與理由，本節僅確認第二輪重提的疑慮
   仍是同一個判定：不成立。

## 對抗審查修復（第三輪，1 條 BLOCKER＋2 條 MAJOR）

1. **`test_projection_does_not_mutate_decision_store_or_registry_bytes` 形同
   空測 fix**（BLOCKER，`tests/test_decision_status_projection_840.py`）：
   AC5「讀取前後 bytes 不變」的 provider 分支呼叫
   `WorkflowRegistryProvider(REPO, state_path=state).scan()` 時沒有注入
   `quota_decision_store=store`——provider 在未注入時會自建一個指向**預設
   路徑**（`quota_admission_decisions_root()/decisions.jsonl`，Trust Root
   資產位置）的 store，跟這裡雜湊的 `tmp_path/decisions.jsonl` 是兩個完全
   不同的檔案；`scan()` 從未真的讀寫過 `store.path`，讓 bytes-不變斷言不管
   scan() 做了什麼都會通過。手動驗證（monkeypatch `scan()` 對它自己持有的
   `self._quota_decision_store.path` 額外寫入 bytes）：未注入時 provider 的
   store path 是預設路徑、corruption 寫進去測試的雜湊完全碰不到，斷言仍
   `True`（vacuous 成立）；注入 `store` 後 provider 的 store path 與測試雜湊
   的檔案是同一個，corruption 被抓到，斷言變成 `False`。修法：明確注入
   `quota_decision_store=store`，並額外斷言 `scan()` 確實投影出這筆
   decision（證明 provider 真的讀到了它，不是又落回『沒有任何 #839 證據』
   的空路徑）。
2. **`quota_admission` 指標沿用已結束 attempt 的舊 receipt fix**（MAJOR，
   `paulsha_cortex/monitor/decision_projection.py`／`providers.py`）：
   `_quota_decision_row()`／`project_workflow_quota_admission()` 過去接受
   `WorkflowRun.quota_admission[persona]` 上的任何指標，不管它是不是這個
   persona『目前』在處理的 attempt——這個指標一旦寫入就留在 run 上，直到
   下一次同一個 persona 的 admission 決策覆寫它為止；`retry-card`（#545／
   #569）重置卡片（`step.executor`／`step.model` 清成 `None`，見
   `registry._manager_reset_workflow_for_retry_card`）後、新 attempt 尚未
   寫出 receipt 前，指標仍指著上一個已經結束的 attempt 的舊 admit／wait
   receipt，`cortex work show`／`cortex inspect status` 會誤呈現成『目前』
   的決策。
   修法：`project_workflow_quota_admission()` 新增可選參數
   `current_identity_by_persona`（呼叫端從 `WorkflowRun.steps` 推導出的
   『每個 persona 目前最早未通過的卡』：`{"card","executor","model"}`，比照
   `registry._manager_reset_workflow_for_retry_card` 挑「當前 phase 內最早
   一張尚未通過的卡」同一個判準）；`_attempt_mismatch_reason()` 比對已找到
   的決策是否仍對應這張卡——`outcome=="admit"` 時要求 `selected` 與目前卡
   的 `executor`／`model_id` 完全相符（兩者本來就在同一次候選迴圈迭代內
   依序寫入，`retry-card` 只清掉 step 這一半，造成不一致）；`outcome==
   "wait"` 時（wait 從未寫入 step 身分）以卡是否相符為僅能拿到的最強訊號；
   卡不同、或呼叫端算不出目前卡（`current_identity_by_persona` 有傳但這個
   persona 沒有任何未通過的卡）一律視為不相符，不臆測。不相符時回報
   `available: false`、`gap_reason: "quota-decision-attempt-superseded"` ＋
   機器可讀的 `mismatch_reason`，不帶出舊 attempt 的
   `mode`／`outcome`／`selected`／`excluded`／`classification`，只保留
   `decision_id` 供 operator 追查那筆已結束 attempt 的舊 receipt。
   **省略此參數維持逐字既有行為**（比照 #835／#839 既有的可選欄位加法
   模式）——本模組既有全部單元測試與 `manager.workflow_status_entry()`
   （`cortex inspect status` 的 `_workflow_quota_decision_projection()`
   呼叫點）都還沒有傳這個參數，因此**只有 `cortex work show`
   （`WorkflowRegistryProvider._quota_decision_row()`，本輪已接上，見下）
   這條路徑受益；`cortex inspect status` 要接上同一個檢查需要在
   `manager.py` 內把 `run.steps` 換算成 `current_identity_by_persona` 並多
   傳一個參數——manager.py 本輪明確排除在改動範圍外（另一票在審），因此
   刻意不動，留下已知的路徑間 parity 缺口，待 manager.py 解除排他後再補
   對稱的一行改動。
   同一輪也修了 `WorkflowRegistryProvider.scan()` 聚合 `quota_decisions` 的
   一個相關缺陷：同一個 `work_id` 對應多個 run（例如舊 attempt 已標
   `done`／`superseded`、換了新 run_id 繼續派工）時，`scan()` 過去無條件
   覆寫、等於『最後一個非空投影贏』——若已終局的舊 run 恰好在 `rows` 迭代
   順序裡排在仍在跑的新 run 之後，就會顯示已經結束的那個 run 的決策。改成
   沿用 `monitor.lifecycle.project_work_items` 既有的選 run 規則（排除
   `done`／`completed`／`failed`／`superseded`，見該函式 `workflows` 變數）：
   只有非終局狀態的 run 才能寫入這個 work_id 的欄位；同一輪掃描裡已有一個
   非終局 run 佔住這個 work_id 時，後來的終局 run 不得覆寫掉它。
   補測試（`tests/test_decision_status_projection_840.py`）：
   shared-function 層級的 admit／wait 相符與不相符（身分 reset、卡片前進、
   算不出目前卡）共 7 個案例、`WorkflowRegistryProvider.scan()` 端到端的
   retry-card 重置情境（`test_provider_scan_after_retry_card_reset_reports_pending_not_stale_admit`）、
   以及 work_id 對應多個 run 時選 run 規則
   （`test_scan_quota_decision_for_work_id_picks_active_run_not_last_processed`）。
   既有 fixture 副作用：本檔 `_step()` 輔助函式的預設 `executor`／`model`
   （`"planned-executor"`／`"planned-model"`）與 `_decision()` 預設
   `selected`（`"codex"`／`"gpt-5.3-codex"`）本來就不相符——新增的比對邏輯
   會讓所有沿用預設值、原本期待完整 admit 投影的既有測試改判成 mismatch。
   `_step()` 新增可選 `executor`／`model` 參數（預設不變），並在會經過
   `WorkflowRegistryProvider.scan()` 的既有測試（`consistency-840`、
   `bytes-840`、`e2e-work-show-840`；另含 `tests/test_monitor_work_api.py`
   的 `test_refresher_quota_decision_cache_survives_across_refresh_ticks_for_last_good_stale`）
   顯式傳入與各自 admit 決策 `selected` 相符的 `executor`／`model`，讓這些
   測試繼續驗證完整投影，而不是意外落入新的 mismatch 分支。
3. **`DecisionReadCache` 被『父目錄權限放寬、檔案本身沒變』繞過 fix**
   （MAJOR，`paulsha_cortex/monitor/decision_projection.py`）：
   `DecisionReadCache._load_index()` 過去只以 store **檔案本身**的
   `(size, mtime_ns, inode, mode)`（`_file_identity()`）判斷快取是否命中，
   完全沒有比對 `AdmissionDecisionStore._check_parent()` 驗的**父目錄**
   權限位（`0o077` 遮罩）。第一次成功讀取後，若把
   `quota-admission-decisions/` 目錄改成不安全權限（例如 `0o777`）——檔案
   本身的 size／mtime／inode／mode 完全不變——`_load_index()` 判定『沒
   變』就直接沿用快取索引，從未真的呼叫 `store.all_rows()`／
   `_check_parent()`，讓已經不安全的目錄繼續回報 `stale=false` 的新鮮
   決策。
   修法：新增 `_parent_identity()`（回傳父目錄的 `(mode, uid, inode)`），
   把它併入 `_load_index()` 的快取鍵（`(檔案身分, 父目錄身分)` 組合）——
   父目錄權限一變，組合鍵就不同，強迫落回真正呼叫 `store.all_rows()`，由
   該呼叫鏈既有的 `_check_parent()` 做同一套 fail-closed 判定（本函式不
   重新定義『安全』的語意，只負責讓比對不被繞過）。補
   `test_decision_read_cache_detects_parent_directory_permission_widened`：
   第一次讀取成功後 chmod 父目錄成 `0o777`，驗證第二次讀取回報
   `stale=true`／`stale_reason` 含 `parent-permissions-invalid`，且 last-good
   內容（同一筆 decision）仍完整保留。

**RED→GREEN 驗證方式**：條目 2／3 用 `git show HEAD:<path>` 暫時還原
`decision_projection.py`／`providers.py`、重跑新測試確認全數失敗（RED），
復原後全數通過（GREEN）。條目 1 因為是測試強度問題（production 程式碼本身
沒有 bug），改用手動 monkeypatch 腳本驗證：讓 `scan()` 對它實際持有的 store
路徑寫入 bytes，比較「未注入 store」與「已注入 store」兩種情境下 bytes-
不變斷言的結果（見上方條目 1 說明的驗證數字）。

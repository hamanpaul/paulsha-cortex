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

新增 19 個測試：`tests/test_decision_status_projection_840.py`，覆蓋多
persona／retry／跨 run-card exact-key、negative（wait 無 Job、unknown
remaining、缺 provenance 的 legacy receipt、needs_human 語意保留、planned
不造 actual）、`workflow_status_entry` 與 `WorkflowRegistryProvider.scan()`
對同一份 snapshot 一致、restart／last-good stale、bytes 不變、allowlist
負面 fixture，以及以真實 `AdmissionDecisionStore` 寫入 receipt 到
`cortex inspect status`／`cortex work show` 文字輸出的端到端案例。

本票刻意不改派工行為、不改 #839 決策邏輯；status 熱路徑只讀既有 Trust Root
資產與 registry row，不評測、不探測 provider。installed／live canary 仍是
獨立部署 gate，未在本票執行。

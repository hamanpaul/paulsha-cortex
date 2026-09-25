---
status: accepted
work_item: diagnostic-main-sync-context
domain_breadth: 0
state_consistency: 1
acceptance_surfaces: 2
spec_stability: 0
orchestration: 2
total_score: 5
sizing: yellow
---

# DiagnosticReason main-sync context 設計

## Decisions

### D1. Additive schema v3 with one typed reserved value

在 `diagnostics.py` 定義 frozen `MainSyncContext` 和其 from/to-dict validators。擴展 `DiagnosticReason` v3 的 `context` JSON projection；key `main_sync` 可承載新 typed object，其餘 context 仍是原 string map。此 typed object 內以 `failure` 為失敗欄位名，精確承載 #987 `MainSyncProbeFailure` 的 `stage`、`returncode`、`error_kind`、`main_head`，並與 live issue AC 的 member path 一致。若讀到 v1/v2 的舊 `context.main_sync` string，保留為 legacy string 並 exact round-trip；不得將它誤判為 typed object。新的 typed writer 一律用明確參數；一份 reason 不可同時設定 legacy `main_sync` string 與 typed object。從 v1/v2 載入時沿用既有欄位並輸出 v3；不要求舊 WorkflowRun migration。

### D2. No conversion or truncation on machine fields

`diagnostic_reason(..., main_sync_context=typed_value)` 專用參數；不得從 `**context` 的 `str(value)` 路徑建構。`DiagnosticReason.from_dict()` 會把 v3 巢狀 JSON 解析為 typed value，`to_dict()` 還原巢狀 mapping/list；`conflict_paths` 每個元素原樣保留。一般 context 的 200 字限制不套到 main_sync 的 typed fields，但 schema 必須拒絕錯型別、未知欄位、v1/v2 中的 nested object、以及無效狀態組合。一般 `context` 的 mapping/list 值一律拒絕；字串仍沿用既有單行化與 200 字上限。

### D3. Hash syntax and Git object truth have separate owners

此 module 對非 null 的 `candidate`、`main_head` 及 `failure.main_head` 檢查 40/64 hex full object-id syntax，並保證 `failure.main_head == context.main_head`。`candidate=null` 僅表示 pre-valid-C failure：`failure` 必填，stage 必須是 #987/#988 共用的精確 literal `candidate-resolve` 或 `candidate-validate`，兩個 main-head 都必須 null、paths 必須為空、repair kind 必須是 `unavailable`、skipped reason 必須為 null。非法／縮寫原始輸入僅由 #987 的 content-addressed delivery evidence 保存，不是 typed Candidate，也不能授權任何 recovery action。Probe 與 action 必須按當前 repo object format 使用 Git 檢查 SHA 長度及 commit object；模型不能僅由看似合法的字串授權 action。

`candidate=null` 的 MainSyncContext 永遠不可用於 `retry-build`。`#989` 必須拒絕並隱藏該 action；`#990` 必須將 null Candidate 與 pre-valid-C failure 寫入同一 WorkflowRun context 並 exact read-back，且 status/action projection 不得宣稱 retry-build 可用。這延伸 #972 的 illegal/missing SHA fail-closed 條件，不放寬原驗收。

### D4. Round-trip proof before integration

建立含多條 >200 字 conflict paths、C/M 與 fetch 後 failure M 的 reason；做 `DiagnosticReason.from_dict(reason.to_dict())` 及 WorkflowRun JSON encode/decode fixture，逐欄斷言巢狀 object、每條 path 和 `failure.main_head=M` 原樣相等。另建立 pre-valid-C fixture：`candidate=null`、stage 分別為 `candidate-resolve`／`candidate-validate`、兩個 main-head 為 null、空 paths、unavailable repair；Reason 與 WorkflowRun JSON read-back 必須逐欄相等。負例拒絕把非法／縮寫原始輸入放入 typed `candidate`、用非 pre-valid-C stage 搭配 null Candidate、null Candidate 搭配非 null main-head／paths／其他 repair kind／skipped reason、pre-valid-C stage 搭配非-null Candidate，以及缺少 failure。該輸入只在 #987 probe evidence fixture 保留；本票不得將它轉成 typed authority。額外載入 v1/v2 fixtures，驗舊 reason 保留、schema 輸出升至 v3；舊 flat `context.main_sync` string 仍是 string。涵蓋 null M、40/64 位合法 SHA、未知欄位、缺欄、failure/main M 不一致與 failure/skip 型別錯誤。Manager store transaction 與 budget/reset-refusal caller 留 child 04；#989/#990 負責驗證 null Candidate 無 retry-build action。

### D5. Sizing (#208)

使用正式 `fix-standard` 9-card combo。domain 0（單一 `diagnostics.py`）；state 1（持久 reason 格式版本化相容，無跨 writer consistency/CAS）；acceptance 2（2 gate_spine + R-09/R-16/R-19）；spec stability 0（三件 accepted artifacts）；orchestration 2（多個 persona-bound cards）。總分 `0+1+2+0+2=5 / Yellow`；以 repository helper 對本目錄三件套重算。

### D6. Scope boundary and dependency

此票 production scope 僅 `paulsha_cortex/coordinator/diagnostics.py`；新增測試可以經現有 `WorkflowRun` serializer 驗 JSON encode/decode，不修改 `workflow.py`、Manager wrapper、recovery actions、CLI 或 probe。#987 尚未完成前不得進入實作或把 schema 視為 probe producer contract；其 producer 必須輸出 `candidate-resolve`／`candidate-validate` 作為 pre-valid-C failure stage。Builder 產物可讀時，在 #988 intake 前逐字核對該 stage literal；不一致先修 #987/#988 契約，不做隱式別名。CLI help 僅檢查沒有新增 CLI surface；如實作後發現需要 CLI 改動，另立有 issue authority 的工作項目。

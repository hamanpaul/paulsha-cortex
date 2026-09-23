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

在 `diagnostics.py` 定義 frozen `MainSyncContext` 和其 from/to-dict validators。擴展 `DiagnosticReason` v3 的 `context` JSON projection；key `main_sync` 可承載新 typed object，其餘 context 仍是原 string map。若讀到 v1/v2 的舊 `context.main_sync` string，保留為 legacy string 並 exact round-trip；不得將它誤判為 typed object。新的 typed writer 一律用明確參數；一份 reason 不可同時設定 legacy `main_sync` string 與 typed object。從 v1/v2 載入時沿用既有欄位並輸出 v3；不要求舊 WorkflowRun migration。

### D2. No conversion or truncation on machine fields

`diagnostic_reason(..., main_sync_context=typed_value)` 專用參數；不得從 `**context` 的 `str(value)` 路徑建構。`DiagnosticReason.from_dict()` 會把巢狀 JSON 解析為 typed value，`to_dict()` 還原巢狀 mapping/list；`conflict_paths` 每個元素原樣保留。一般 context 的 200 字限制不套到 main_sync 的 typed fields，但 schema 必須拒絕錯型別、未知欄位與無效狀態組合。

### D3. Hash syntax and Git object truth have separate owners

此 module 檢查 40/64 hex full object-id syntax，並保證 `probe_failure.main_head == context.main_head`。Probe 與 action 必須按當前 repo object format 使用 Git 檢查 SHA 長度及 commit object；模型不能僅由看似合法的字串授權 action。

### D4. Round-trip proof before integration

建立含 3 個 >200 字 conflict paths、C/M 與 fetch 後 failure M 的 reason；做 `DiagnosticReason.from_dict(reason.to_dict())` 及 WorkflowRun JSON encode/decode fixture，斷言巢狀 object 與每條 path 原樣相等。額外載入 v1/v2 fixtures，驗舊 reason 不變且 schema 輸出升至 v3。Manager store transaction 和 budget/reset-refusal caller 留 child 04。

### D5. Sizing (#208)

使用正式 `fix-standard` 9-card combo。domain 0（單一 `diagnostics.py`）；state 1（持久 reason 格式版本化相容，無跨 writer consistency/CAS）；acceptance 2（2 gate_spine + R-09/R-16/R-19）；spec stability 0（三件 accepted artifacts）；orchestration 2（多個 persona-bound cards）。總分 `0+1+2+0+2=5 / Yellow`；以 repository helper 對本目錄三件套重算。

---
type: feat
scope: coordinator
---

新增 `paulsha_cortex/coordinator/quota_collectors.py`：#836 唯讀 provider 額度
讀取器，讓 operator 能產生真實觀測供 `quota_shadow`／`quota_ledger` 使用（先前
只有 `provider_read_contract()`／`capture_provider_quota()`，沒有任何程式真的
去讀 provider，production ledger 永遠沒有觀測）。codex 走 `codex app-server`
stdio JSON-RPC（`initialize`→`initialized`→`account/rateLimits/read`），整體
逾時（預設 20 秒）或協定失敗一律終止子程序（terminate→kill）並回精確 gap；
agy 執行 `agy -p /usage --output-format json` 一次性讀取；copilot 已用
`copilot --help` 核對其子命令，目前沒有非互動唯讀路徑，回報
`copilot-quota-read-path-unavailable`（不猜測、不爬網頁）；claude／cg 沿用既有
contract 的 unsupported／unknown 狀態。不讀任何 credential 檔，也不把 provider
原始 payload 或帳號識別外洩到 ledger／stdout／錯誤訊息，只有
`capture_provider_quota()` 產出的已去識別 observation 才會往下游走。

新增 `quota_collectors.load_collector_config()`：讀取版本化的
`cortex/quota-pools/v1` 設定檔（`config_revision`／`descriptors`／
`unit_catalog`／`bindings`，選填 `lease_ms`），descriptor／binding 驗證完全
交給既有的 `quota_observation.parse_*`；額外解析選填的 `collector_targets`
區塊為依 profile_key 分組的 `ProviderQuotaTarget`。設定檔路徑一律由呼叫端
明確給定，不動 `paths.py` 既有預設值，避免與另一票新增
`quota_pools_config_path()` 衝突。

新增 CLI `cortex quota observe --config <path> [--executor ...] [--dry-run]
[--json] [--output <file>] [--timeout-s <秒數>]` 與
`cortex quota import --config <path> --file <file>`：`observe` 預設寫入
Manager 帳號的 quota event ledger，路徑不可寫時（多 UID 部署下執行者不是
Manager 帳號）回報 `ledger-unwritable` 並建議改用 `--output`；`import` 由
Manager 帳號以 `record_external_observation()` 匯入 `--output` 落地的檔案，
拒絕非唯讀 `method`。README 補上對應段落。

以 fake runner（模擬 codex／agy 真實回應形狀與逾時／非零 exit／壞 JSON／缺
id=2 回應）驗證 observation 正確、子程序確實被終止、accountId／原始 payload
不外洩、ledger 不可寫時不崩潰、export→import 往返等價；測試中不呼叫真正的
provider CLI。以真實 `codex app-server --dry-run --json` 與本機可用的 `agy`
各跑一次唯讀 dry-run 驗證實際回應形狀；copilot 目前無對應唯讀 CLI 路徑。

## 對抗審查第九輪：三條 MAJOR 修復

1. **`_parse_codex()` 只認頂層 `rateLimits`**：回應只有
   `rateLimitsByLimitId`（`qualification/driver.py` 的 preflight 早就接受
   這種形狀）時，所有 target 會誤判成 `invalid-provider-value`。改為對每個
   target 的 `codex:<limitId>:<providerWindowName>` 同時查
   `rateLimitsByLimitId[limitId]` 與頂層 `rateLimits`（其 `limitId` 需相
   符）——任一處有值即採用，兩處都給出非 null 值但不同才算矛盾，回精確的
   `provider-limit-snapshot-conflict`（不猜哪一份才對）。另外
   `secondary: null` 這種「provider 沒有這個 window」改回精確的
   `provider-window-absent`，不再歸成 `invalid-provider-value`。
   實作細節：`providerWindowName`（provider 回應裡的欄位名，如
   `primary`／`secondary`）跟本地 `target.window_id`（descriptor 端任意
   operator 命名）是兩個獨立識別，`_parse_codex()` 從 `resource_key` 反解
   `providerWindowName`，`target.window_id` 只用來對 descriptor 找
   `duration_ms` 期望值——沿用既有測試既有的解耦設計，不能假設兩者字面相同。
2. **忽略 `ordinaryUsageAllowed`／`rateLimitReachedType`／
   `spendControlReached`**：provider 已明確拒絕一般用量時，`_parse_codex()`
   仍只看 `usedPercent` 寫出「有剩餘」的觀測。新增 `_codex_window_value()`：
   三個 gating 欄位任一觸發（`ordinaryUsageAllowed is False`／
   `rateLimitReachedType` 非 null／`spendControlReached is True`）時，該
   limitId 下所有 window 改記為 remaining `0`（`provider-reports-limit-
   reached`，保留 `resetsAt`）——選擇回報固定值 0 而非 `unknown`，因為 0
   永遠不會高估剩餘量，且比空白的 unknown 更能讓下游知道「目前不可用」；
   欄位型別不符（非 bool／非 null 字串）回精確的 `invalid-provider-value`。
   `ordinaryUsageAllowed` 是整個回應層級的欄位（跟 `rateLimits` 平行，不巢
   在 limitId snapshot 裡），`rateLimitReachedType`／`spendControlReached`
   才是巢在 limitId snapshot 底下的欄位——两者巢狀層級不同，不能用同一組
   key 位置去讀。`credits` 欄位沒有到 rate-limit-percent 的驗證過 unit
   mapping，不換算成額度，也不參與 gating 判斷。
3. **`cortex quota import` 只驗 top-level schema**：手工構造的
   `provider_status` 檔（`source_*`／`authority_ref` 可隨意填）會被直接
   接受並寫入 ledger。新增 `quota_collectors.validate_import_payload()`：
   確認匯入檔是 `observe --output` 產生的 export envelope
   （`schema`／新增的 `collector_version`／`config_revision` 固定形狀），
   且每筆 observation 都先過 `parse_observation()`、`source.method` 為唯讀
   分類、`source_schema`／`authority_ref`／unit 的 `semantics_ref` 逐字等於
   依 `source_id` 反查 executor 後 `provider_read_contract(executor)` 的
   正式值、`config_revision` 等於目前 `--config`、
   `(pool_ref, window_id, profile_key)` 屬於該 executor 的
   `collector_targets` 白名單、`observed_at_ms` 不在未來也未超過自身
   `ttl_ms` 過期；任一筆不符即整份拒絕（不部分匯入），錯誤訊息只帶
   `[a-z][a-z0-9-]*` 形狀的 reason code，絕不含原始內容。`_run_import()`
   改先呼叫這個驗證函式，通過才走既有的 `record_external_observation()`
   逐筆寫入。`collector_version` 只驗證「envelope 有沒有這個欄位」，不要求
   跟目前執行版本相等，避免擋掉跨版本升級時的合法匯入。README 補上信任邊界
   段落：import 假設執行者是 Manager 帳號、檔案由 collector 產生，上述檢查
   擋不掉「持有 operator 執行權限者刻意偽造整份檔案」（偽造者能自己先跑一次
   `observe --output` 取得所有欄位合法值再改動內容），要防這一層需要 collector
   對輸出簽章、import 端驗簽，目前尚未實作，另議。

新增測試：`tests/test_quota_collectors_836.py` 補上 rateLimitsByLimitId-only、
兩處矛盾／不矛盾、三個 gating 欄位分別觸發與型別不符、`validate_import_payload()`
的正常/竄改 source contract／config_revision／resource_key／observed_at 未來
或過期／缺 `collector_version` 各案例，以及一則走完整 CLI 路徑的偽造檔拒絕
測試；既有 `test_codex_real_response_shape_produces_observation_and_terminates_process`
的 `secondary` 斷言同步改為 `provider-window-absent`。以
`git show HEAD:<path>` 暫時還原對應 production 檔重跑新測試確認 RED，復原後
再確認全數 GREEN；`tests/test_quota_observation_refine_836.py` 全數維持通過
（其中兩則既有測試的 `resource_key`／`window_id` 刻意不同名，證實了
providerWindowName／window_id 解耦設計不能破壞）。最後以真實
`codex app-server`（`cortex quota observe --dry-run --json`）唯讀重跑一次
確認仍能取得 primary observation。

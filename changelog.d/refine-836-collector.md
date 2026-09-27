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

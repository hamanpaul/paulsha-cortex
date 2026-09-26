type: feat
scope: coordinator
---

沿用 #866 quota observation schema-core，完成 #836 shadow feature group：Codex
App Server、Copilot SDK、Antigravity JSON payload adapter；未有正式 quota read
介面的 executor 以 unknown 與 coverage gap 表示。新增 Manager-owned append-only
quota event ledger，支援 event/caller replay 去重、來源衝突收據、TTL/reset/clock
rollback reconciliation 與損毀 fail-closed；新增 terminal registry usage consumer，
保留原生 unit/source provenance，無 mapping 時不扣 subscription quota。增加多 pool、
多 window、overlap window、concurrency gauge 的唯讀 shadow projection 與 Trust Root
path/permission 註冊。未連接 workflow chain、sorting、admission 或 dispatch；外部
provider live read、部署目錄權限與 runtime/canary 驗收仍待完成。

修正 Codex App Server rate-limit adapter，接受 protocol v2 camelCase 欄位並保留
snake_case 舊 payload 相容；可選的 `windowDurationMins`／`resetsAt` 缺值時，不丟棄
`usedPercent` 的有效 remaining，reset 時間明確為 unknown。provider 未提供 event ID 時，
ledger 改以穩定的來源、
scope/window、measurement 與 observation 時間推導 identity；相同 identity 的內容
衝突會寫 conflict receipt 並使 shadow remaining 保持 unknown。補上 reset 已過但 TTL
未到的 freshness regression，確認此情況已由既有 freshness/min-expiry 邏輯 fail closed。

修正 shadow projection：remaining snapshot 的 window epoch 未知時，後續同 unit usage
不再與 snapshot 合成確定餘額；projection 回報 unknown 並附 `window-epoch-unknown` gap。
已知且相符的 window epoch 仍可扣算 usage。

修正終局 usage 跨越 snapshot 時的重複扣減：ledger 新增 `terminal_job_started_at_ms`，
隨終局 usage observation 一併記入且納入 payload digest；shadow projection 只在
job 已知開始時間不早於該 snapshot 的 observed_at 時，才視為整筆消耗都落在
snapshot 之後、可安全整筆扣減。已知開始時間早於 snapshot（跨越 snapshot）時不再
整筆扣除，projection 回報 unknown 並附 `straddling-usage` gap；schema 目前尚未支援
可切分的增量事件，故此情形無法精確扣減，僅能保守標示 unknown。

修正終局 usage 一律無法投影扣減的問題：`record_terminal_usage()` 之前把
`window_instance` 永遠標 unknown，即使已有含 reset 的 fresh snapshot 且 job
完整落在該窗口內，也只會得到 `usage-window-unresolved` 而不會扣減。現在依
job 的 started_at／finished_at，比對 ledger 內已知 remaining snapshot 的
window instance（interval 起訖）；唯一且完整覆蓋 job 起訖時間才標 known，
跨窗口或資訊不足一律 unknown，不臆測。新增以正常 producer 路徑
（`record_terminal_usage`）驗證可扣減的端到端測試。

修正終局 usage 的 replay key 只含 job_id/profile_key/metric/window_id，導致
同一 binding 把同 metric/unit 映到兩個 pool、且 window_id 恰好同名（例如
shared-account 多角色都叫 `month`）時，第二筆會誤判成與第一筆衝突、錯拆池。
replay key 現在納入 pool identity（含 revision）的雜湊。

修正終局 usage 的 `observation_id` 與 payload digest 把記錄當下的
`observed_at_ms`/`received_at_ms` 烘入身分，導致重啟後同一個已結束 job 若以
較晚時間重播相同 usage，會被誤判成 conflict 而非 duplicate。終局 usage 的
identity／digest 現在只綁 usage 內容與 job 開始時間等終局事實，不綁重播當下
的記錄時間；內容真的不同時仍正確判定為 conflict。

修正 `project()` 用當前傳入的 descriptors/unit_catalog 重新解析整份
append-only ledger、任何解析失敗（例如 pool 升版後 caller 只帶新 revision，
ledger 內舊 revision 事件對不上）都會被當成 `ledger-corrupt` 毒化整份 shadow
projection、連未變動的其他 pool 也遭殃。現在只有 ledger 檔案本身結構損毀
才會整份標 `ledger-corrupt`；個別事件解析失敗改為逐筆處理，只讓對應的 pool
標記 `stale-pool-revision`，不影響其他 pool 的投影結果。

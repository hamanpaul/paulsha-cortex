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

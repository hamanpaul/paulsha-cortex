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

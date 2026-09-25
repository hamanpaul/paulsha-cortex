---
status: draft
work_item: monitor-refresh-attempt-ledger
issue: 1077
---

# Monitor refresh attempt ledger 設計

## Decisions

### D1 — Versioned sidecar record

在 `WorkSnapshotStore` instance-scoped directory旁新增 strict, versioned attempt-marker file。marker包含 generation、per-repo outcome、attempt time與 diagnostic；它與 last-good payload分離。未知 schema、損壞、read/write failure不得自動重建。

### D2 — Begin before scan

每次 refresh 在單一 writer lock內讀取並驗證 durable marker，generation加一後先 atomic durable-write `running`，再呼叫 provider/correlation。已存在的成功因此在新 attempt開始時立即失效。

### D3 — Failure publication and staged success

本 slice將該代失敗寫為 durable `failed`；程序在 terminal write前中斷則保留 `running`。在 #1078 完成 manifest/read-back contract 前，不發布可供 freshness API採信的成功結果；不完整的中間代保持 untrusted。

### D4 — Restart and concurrency

restart沿用 durable generation，不能回到零。若同一 instance directory可能有跨 process writer，使用 durable lock/CAS；無法取得唯一 writer時拒絕 refresh並保留可診斷狀態。

## Open Questions

- 無。

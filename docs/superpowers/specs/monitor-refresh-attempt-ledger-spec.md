---
status: draft
work_item: monitor-refresh-attempt-ledger
issue: 1077
---

# Monitor refresh attempt ledger 規格

## Requirements

### R1 — 每個 refresh 先持久化 generation

Monitor 每次開始 correlation refresh，MUST 從已驗證的 durable attempt marker 配置嚴格遞增 generation，並在任何 provider scan 前寫入 `running`。generation 是 refresh attempt identity，不得由 `WorkSnapshot.sequence`、`written_at` 或 wall clock 推算。

### R2 — failure outcome 與 last-good rows 分離

每 repo 最新 attempt marker MUST 獨立於 last-good `WorkSnapshot` rows。provider/correlation failure、exception 或 degraded outcome MUST 留下 durable failure result；既有 rows 可以留作診斷，但不得表示最新 attempt 成功。

### R3 — 中斷與未知狀態 fail closed

restart 後仍為 `running` 的 generation、missing/legacy/unknown/malformed marker 與 marker I/O error MUST 不可信且不得重置 generation。marker allocation/publication 由同 instance 單一 writer 序列化；若多 process 可寫同一 store，必須使用 durable lock/CAS，否則拒絕第二 writer。

### R4 — Slice boundary

本票交付 generation allocation、running marker與 failure transitions。generation 成功狀態的 input/source manifest、snapshot read-back與 freshness API由 #1078 負責；source qualification/path admission由 #1063 負責。

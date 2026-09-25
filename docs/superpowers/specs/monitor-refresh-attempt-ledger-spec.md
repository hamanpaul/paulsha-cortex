---
status: draft
work_item: monitor-refresh-attempt-ledger
issue: 1077
---

# Monitor refresh attempt ledger 規格

## Problem and Outcome

`WorkSnapshotStore` 持久保存 last-good rows；目前 refresh exception 主要由記憶體中的 `WorkReadModelStore` 記錄。Monitor restart 後，durable snapshot 不能獨立說明最近一次 correlation refresh 是否開始、失敗或中斷。需要一筆與 snapshot 分離的 durable attempt ledger，讓最新 attempt 狀態不會被舊的成功 rows 遮蔽。

本票只交付 generation allocation、scan 前的 `running` marker，以及 per-repo failure/degraded/exception outcome。完整成功證明需由 #1078 綁定 exact inputs、sources 與 snapshot read-back；本票不發布可供 freshness consumer 採信的成功狀態。

## Requirements

### R1 — 每次 refresh 先持久化單調 generation

Monitor 每次開始 correlation refresh MUST 從 strict-validated durable attempt marker 配置嚴格大於前一代的 generation，並在任何 provider scan 或 correlation 前 atomic durable-write `running`。Generation 是 attempt identity，不得從 `WorkSnapshot.sequence`、`written_at` 或 wall clock 推算。Marker allocation 或 `running` write 失敗時 MUST 停止該 refresh；不得回退 generation zero 或採信舊成功 rows。

### R2 — Per-repo failure outcome 與 last-good rows 分離

每個 repo 在該 generation 的 `failed`、`degraded` 或 `exception` outcome MUST 寫入獨立於 `WorkSnapshot` 的 durable marker，並帶足以診斷的穩定 outcome 與錯誤摘要。某 repo 失敗不得將其他 repo 的結果誤標成功或失敗。Last-good rows MAY 保留供診斷，但 MUST NOT 代表最新 attempt 成功。

### R3 — Crash、未知資料與 I/O failure fail closed

Restart 後仍為 `running` 的 generation MUST 保持 untrusted；未知版本、malformed marker、marker read/write error MUST fail closed，且不得自動重建或回退 generation。第一次建立 store 時可由明確的 empty-store 初始化規則配置首代；若已有 snapshot 卻缺少有效 marker，MUST 視為 legacy/unknown 並拒絕以零代重新開始。

### R4 — 成功結果保留給下一個 producer slice

Provider/correlation scan 完成但尚未完成 #1078 的 input/source manifest 與 snapshot read-back 時，該 generation MUST 維持 untrusted（可標為 `awaiting-success-evidence`）；MUST NOT 發布 `succeeded`。#1078 才負責 success marker、exact source/snapshot binding 與 freshness API。

### R5 — 保留既有 owner boundary

本票依賴 #1063 提供 canonical Todo source/path qualification；MUST NOT 重做 source qualification 或 path admission。MUST NOT 修改 #1065 WorkAuthority consumer、#1054 Manager gate、CLI、recovery 或 ship behavior。

## Scenarios

### 新 refresh 先留下 running generation

- **WHEN** Monitor 準備執行任何 repo provider scan
- **THEN** durable marker 已含比前次更大的 generation 與 `running`，marker 寫入成功前不開始 scan

### Failure 後舊 rows 仍只供診斷

- **WHEN** repo 的新 generation 發生 provider failure、degraded correlation 或 exception，而舊 snapshot 仍含 matching work row
- **THEN** 最新 generation 的 repo outcome 已獨立持久化為 failure/degraded/exception，舊 row 不會把該 generation 表示成成功

### Restart 遇到 running 或未知 marker

- **WHEN** restart 讀到 crash 留下的 `running`、未知版本、malformed marker 或 marker I/O error
- **THEN** generation 不歸零、不回退到舊 success，且最新 attempt 維持 untrusted

### Success evidence 尚未由 #1078 實作

- **WHEN** scan/correlation 完成但尚未有 exact input/source manifest 與 durable snapshot read-back
- **THEN** attempt 保持 `awaiting-success-evidence` 或等價 untrusted 狀態，不發布可供 consumer 採信的成功 marker

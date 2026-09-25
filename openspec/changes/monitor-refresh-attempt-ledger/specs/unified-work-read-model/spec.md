## ADDED Requirements

### Requirement: 每次 Monitor correlation refresh 須先持久化 attempt generation

Monitor MUST 為每次 correlation refresh配置嚴格遞增的 durable generation，並在任何 provider scan 或 correlation 前將最新 generation 原子寫為 `running`。Marker MUST 獨立於 last-good `WorkSnapshot`。Marker allocation/read/write 未通過時 MUST 不開始或中止該次 refresh，且 MUST NOT 以 snapshot sequence、timestamp 或舊 rows 回退推論成功。

#### Scenario: Scan 前 marker 已 durable

- **WHEN** Monitor 開始一輪 correlation refresh
- **THEN** marker 已保存新 generation 與 `running`
- **AND** marker durable-write 完成前沒有 provider scan/correlation 發生

### Requirement: Latest per-repo failure MUST 獨立持久化

Monitor MUST 為該 generation 的每個 repo 獨立持久化 `failed`、`degraded` 或 `exception` outcome。單一 repo failure MUST NOT 把另一 repo 的 outcome 改成成功或失敗。Last-good `WorkSnapshot` rows MAY 保留供診斷，但 MUST NOT 使最新 generation 看似成功。

#### Scenario: Failure follows a matching last-good row

- **WHEN** last-good snapshot 含 matching Todo row，而該 repo 最新 refresh 發生 provider failure、degraded correlation 或 exception
- **THEN** marker 保存該 repo 最新 failure outcome
- **AND** 舊 row 仍可作診斷但不表示最新 attempt 成功

#### Scenario: One repo fails in a multi-repo refresh

- **WHEN** 同一 generation 中一個 repo 失敗而另一 repo 有獨立結果
- **THEN** failure marker 精確對應失敗 repo，不串改另一 repo 的 outcome

### Requirement: Interrupted, malformed, unknown, and I/O-failed markers MUST fail closed

Restart 後仍為 `running` 的 generation MUST 保持 untrusted。Unknown schema、malformed marker、generation invalid、marker read/write failure MUST NOT reset generation、覆寫 unknown state 或回退採信舊 success。只有明確的首次 empty-store 初始化可配置首代；已有 snapshot 卻缺少 marker MUST 視為 legacy/unknown。

#### Scenario: Crash leaves running marker

- **WHEN** process 在 `running` durable-write 後、failure/success-evidence outcome 寫入前停止
- **THEN** restart 保留該 generation 為 untrusted，不回退至前一代

#### Scenario: Unknown marker cannot be reset

- **WHEN** marker schema/version 不認得、內容 malformed 或 marker I/O 失敗
- **THEN** refresh 拒絕把 generation 當成零並繼續使用 last-good rows

### Requirement: Attempt ledger MUST preserve adjacent ownership boundaries

本 producer MUST 消費 #1063 提供的 canonical source/path facts，不重做 qualification/path admission；MUST NOT 實作成功 input/source manifest、snapshot read-back 或 trusted freshness API（#1078）、WorkAuthority consumer（#1065）或 Manager admission（#1054）。沒有 #1078 所需證據的完成 scan MUST 維持 untrusted。

#### Scenario: Scan completed before success evidence exists

- **WHEN** provider/correlation scan 完成但 exact source/input revisions 或 snapshot read-back 尚未驗證
- **THEN** generation 保持 non-trusted `awaiting-success-evidence` 或等價狀態
- **AND** 本 issue 不發布 `succeeded` 或 freshness result

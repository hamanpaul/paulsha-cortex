---
status: draft
work_item: monitor-refresh-attempt-ledger
issue: 1077
---

# Monitor refresh attempt ledger

## Why

Monitor 的 `WorkSnapshot` 是 durable last-good store，但 refresh exception 目前主要記在記憶體中的 `WorkReadModelStore`。Restart 後，舊 snapshot rows 仍可存在，卻不能證明最新 correlation attempt 是否開始或失敗。需要在 provider scan 前先 durable-write 單調 generation，並將 per-repo failure/degraded/exception 狀態存在 snapshot 之外，讓最新失敗不能被舊 rows 遮蔽。

## What Changes

- 增加與 `WorkSnapshot` 分離的 versioned attempt marker 與單調 generation allocation。
- 每輪 provider scan/correlation 前先持久化 `running`；marker allocation/write 失敗時停止 refresh。
- 將 per-repo failure、degraded、exception outcome 持久化；保留 last-good rows 作診斷，但不以其推論 latest success。
- 對 crash 留下的 `running`、unknown/malformed marker 與 marker I/O failure 採 fail-closed，禁止 generation reset。
- 依賴 #1063 source/path contract；將 exact input/source revision、durable snapshot read-back、trusted success marker/API 留給 #1078。

## Requirements

- Every refresh allocates a strictly increasing durable generation and records `running` before any provider scan or correlation.
- Per-repo failure, degraded, and exception outcomes are persisted independently of last-good `WorkSnapshot` rows.
- Interrupted, unknown, malformed, or unreadable/unwritable markers remain untrusted and never reset generation.
- This issue does not publish trusted success; #1078 owns input/source binding, durable snapshot read-back, and freshness API.

## Capabilities

### Modified Capabilities

- `unified-work-read-model`: 增加 durable refresh attempt 與 per-repo failure outcome 契約。

### New Capabilities

None.

## Impact

- 預定 production modules：`paulsha_cortex/monitor/work_api.py` 與 `paulsha_cortex/monitor/work_snapshot.py`。
- 預定 isolated regression coverage：Monitor attempt-marker store 與 refresh orchestration 的 generation/failure/restart/I/O 行為。
- 不新增 CLI，不修改 WorkAuthority、Manager、recovery 或 ship semantics；不包含正式 intake、runtime implementation 或 deployment。

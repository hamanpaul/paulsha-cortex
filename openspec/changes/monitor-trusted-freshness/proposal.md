---
status: draft
work_item: monitor-trusted-freshness
issue: 1078
---

# Monitor trusted freshness API

## Why

Monitor 的 last-good `WorkSnapshot` row、`sequence` 與記憶體 refresh 狀態都不能證明最新成功 correlation 使用目前 `.cortex/work-items.yaml` 或同一份 provider/source snapshot。attempt generation 由 #1077 提供；本 issue 需要將 success 綁定 exact consumed input/source revisions 和 durable snapshot read-back，讓 Monitor 能以單一唯讀 API 證明或拒絕 repo/work freshness。

本 change 是 issue [#1078](https://github.com/hamanpaul/paulsha-cortex/issues/1078) 的 draft planning。它依賴 #1063 的 canonical source/path contract 與 #1077 durable attempt ledger。順序為 #1063 → #1077 → #1078 → #1064 umbrella completion → #1065 WorkAuthority consumer → #1054 Manager admission。

## What Changes

- Correlation 對 `.cortex/work-items.yaml` 一次讀取 raw bytes，計算 revision 並解析同一份 bytes；missing input 使用穩定 absent revision，任何 parse/read error 不產生成功 evidence。
- 每 repo success manifest 記錄同 generation 實際使用的 provider revisions 及 `source_id → revision`，不混入 last-good source 或另一次 scan。
- Snapshot candidate durable-write 後 reload，核對 canonical digest、sequence、repo/work rows、ownership 和 revisions；通過後才透過 #1077 marker contract 發布同 generation success。
- 新增一個 Monitor read-only repo/work freshness API，使用 canonical configured repo root 與 `stale_after_seconds`，回傳 typed trust verdict、reason 與已驗證 evidence。
- Missing、legacy、unknown、running、failed、stale、mismatch、malformed、ambiguous-root 與 I/O error 一律 fail closed。

## Capabilities

### New Capabilities

- `monitor-trusted-freshness`: exact correlation input/source capture、success marker 與 durable snapshot read-back 的 same-generation binding，以及單一唯讀 freshness API。

### Modified Capabilities

無。本 draft 為 #1078 slice 自有 capability；#1064 umbrella 另保有端到端 producer capability 視圖。

## Impact

- 預定 production modules：`paulsha_cortex/monitor/correlation.py`、`work_api.py`、`work_snapshot.py`；不得新建或複製 #1077 generation allocator/marker writer。
- 預定 isolated tests：Monitor correlation input revision、snapshot read-back、同代 marker publication 與 typed freshness API。
- 不改 WorkAuthority reader、Manager admission、CLI、source qualification/path admission、recovery 或 ship semantics。
- 無外部依賴、deployment 或資料 migration；缺少可信 success evidence 的舊 snapshot 僅供既有 diagnostics/listing，API fail closed。

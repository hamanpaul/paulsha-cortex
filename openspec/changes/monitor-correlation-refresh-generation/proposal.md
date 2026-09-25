---
status: draft
work_item: monitor-correlation-refresh-generation
issue: 1064
---

## Why

Monitor的`WorkSnapshot`保留last-good rows，但最新refresh failure marker目前只存在`WorkReadModelStore`記憶體；現存matching Todo row可能在後續refresh失敗後仍被讀者採信。snapshot也無法證明成功correlation使用的是目前`.cortex/work-items.yaml`及同一source snapshot的revision，因此需要獨立、單調的attempt generation與read-back綁定，讓Monitor能以一個trusted API證明或拒絕repo/work freshness。

本change是producer umbrella issue [#1064](https://github.com/hamanpaul/paulsha-cortex/issues/1064)的draft規劃。實作依賴順序為#1063 source qualification/path contract → [#1077](https://github.com/hamanpaul/paulsha-cortex/issues/1077) durable attempt ledger → [#1078](https://github.com/hamanpaul/paulsha-cortex/issues/1078) source/snapshot binding與freshness API → #1064 umbrella completion → #1065 WorkAuthority consumer → #1054 Manager gate。此PR只發布規劃，不實作runtime behavior。

## What Changes

- 每次Monitor correlation refresh先持久化單調generation與running outcome，成功、失敗、部分repo失敗及crash window均可讀。
- latest attempt marker獨立於last-good WorkSnapshot payload；成功marker保存實際override input與source revisions。
- success marker綁定durable WorkSnapshot read-back digest/sequence；未完成、未知、失敗或不一致均不可採信。
- 新增單一唯讀repo/work freshness API，驗證current correlation input、source revisions、同generation snapshot及明確age bound。
- 保持#1063 source/path資格、#1065 WorkAuthority consumption與#1054 Manager admission各自的owner boundary。

## Requirements

- Monitor must persist a monotonically increasing attempt generation and independent latest-outcome marker before scanning.
- A successful repo outcome must bind the exact correlation input and source revisions to a durable WorkSnapshot read-back.
- A single read-only freshness API must fail closed for incomplete, failed, stale, legacy, or inconsistent evidence.
- The producer depends on #1063; #1065 owns WorkAuthority consumption and #1054 owns Manager admission.
- Implementation is split into ordered producer issues #1077 (attempt/failure ledger) and #1078 (successful input/source/snapshot evidence and freshness API); #1064 remains their umbrella.

## Capabilities

### New Capabilities

- `monitor-correlation-refresh-generation`: generation attempt outcomes、independent failure marker、source snapshot read-back及Monitor freshness API。

### Modified Capabilities

None. Repo沒有需修改的canonical OpenSpec capability；本change新增一項能力。

## Impact

- 預定production modules：`paulsha_cortex/monitor/work_api.py`、`work_snapshot.py`、`correlation.py`。
- 新增isolated regression coverage：`tests/test_monitor_correlation_refresh_generation.py`。
- Planned ownership split：#1077 owns attempt generation/failure persistence；#1078 owns exact input/source capture、durable snapshot read-back與trusted freshness API。
- 未來需補Monitor API documentation、changelog fragment與Unreleased entry；無CLI command或Manager/claim/WorkAuthority consumer變更。
- 無外部依賴、deployment或資料migration；舊snapshot無marker時只可供既有diagnostic/listing使用，trusted API fail closed直到一次完整成功refresh。

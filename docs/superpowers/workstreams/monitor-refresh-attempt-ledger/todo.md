---
status: draft
work_item: monitor-refresh-attempt-ledger
issue: 1077
domain_breadth: 1
state_consistency: 1
invariant_count: 4
artifact_classes:
  - source
  - tests
  - documentation
---

# Monitor refresh attempt ledger 工作計畫

## Binding

唯一 owner 是 [issue #1077](https://github.com/hamanpaul/paulsha-cortex/issues/1077)，唯一 `work_item` 為 `monitor-refresh-attempt-ledger`。本票是 #1064 producer umbrella 的第一個 implementation slice；#1063 提供前置 source/path contract，#1078 在本票後綁定 successful evidence，#1064 才能整體交付，再由 #1065 消費，#1054 保有 Manager admission。

## Boundary

只修改 Monitor refresh generation allocation、durable running/failure marker、writer serialization及其 focused tests。成功 freshness證明由 #1078 負責；不改 correlation source qualification、WorkAuthority、Manager、CLI或 recovery。

## Sizing and Gate Status

- 官方計算器：`paulsha_cortex.coordinator.work_bridge.current_sizing_snapshot()` / `fix-standard`；本 slice 的 sizing rows 是本 todo與同 work item 的 Superpowers spec/design。OpenSpec capability delta仍由 #1064 umbrella維護。
- 目前 draft helper 應為 **8/10 Red**：`domain_breadth=1`、`state_consistency=1`、`acceptance_surfaces=2`、`spec_stability=2`、`orchestration=2`。
- 只在暫存副本將三份 sizing rows 的 frontmatter status改為 accepted，官方 completeness預期 complete；accepted-status counterfactual為 **6/10 Yellow**（1+1+2+0+2）。這是 sizing投影，不是目前接受、intake或實作授權。
- #1063→#1077→#1078→#1064→#1065→#1054 是硬依賴順序；本票不執行 intake。

## Tasks

- [ ] 持久化並嚴格驗證 versioned generation marker，在 scan前 durable-write running。
- [ ] 將 per-repo failure / degraded outcome寫入 marker；保留 last-good rows但不表示最新成功。
- [ ] 驗證 crash/restart、unknown/corrupt marker、read/write error及單 writer行為。
- [ ] focused tests使用 isolated temp stores、fake providers與 fake clock，不觸碰正式 Monitor state、GitHub或模型。

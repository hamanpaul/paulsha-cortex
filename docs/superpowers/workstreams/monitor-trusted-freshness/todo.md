---
status: draft
work_item: monitor-trusted-freshness
issue: 1078
domain_breadth: 1
state_consistency: 1
invariant_count: 4
artifact_classes:
  - source
  - tests
  - documentation
---

# Monitor trusted freshness 工作計畫

## Binding

唯一 owner 是 [issue #1078](https://github.com/hamanpaul/paulsha-cortex/issues/1078)，唯一 `work_item` 為 `monitor-trusted-freshness`。此為 #1064 producer umbrella 的第二個 implementation slice，依賴 #1077 marker ledger與 #1063 canonical input/source boundary；完成後才可交付 #1064，#1065接 WorkAuthority，#1054保有 Manager gate。

## Boundary

只擷取實際 correlation input/source revisions、durable WorkSnapshot read-back、generation success publication與唯讀 Monitor freshness API。marker allocation/failure persistence由 #1077負責。不得修改 WorkAuthority/Manager或 source qualification/path guard。

## Sizing and Gate Status

- 官方計算器：`paulsha_cortex.coordinator.work_bridge.current_sizing_snapshot()` / `fix-standard`；本 slice 的 sizing rows 是本 todo與同 work item 的 Superpowers spec/design。OpenSpec capability delta仍由 #1064 umbrella維護。
- 目前 draft helper應為 **8/10 Red**：`domain_breadth=1`、`state_consistency=1`、`acceptance_surfaces=2`、`spec_stability=2`、`orchestration=2`。
- 只在暫存副本將三份 sizing rows的 frontmatter status改為 accepted且其他內容不變，官方 completeness預期 complete；accepted-status counterfactual為 **6/10 Yellow**（1+1+2+0+2）。這是 sizing投影，不是目前接受、intake或實作授權。
- #1063→#1077→#1078→#1064→#1065→#1054 是硬依賴順序；本票不執行 intake。

## Tasks

- [ ] Capture exact parsed input revision and same-generation provider/source revision map.
- [ ] Durably write and reload snapshot; verify digest, sequence, rows and ownership before success marker.
- [ ] Implement one read-only freshness API with typed fail-closed reasons and configured age bound.
- [ ] Cover success, input drift, source/read-back mismatch, legacy/unknown marker, degraded provider and stale evidence with isolated tests.

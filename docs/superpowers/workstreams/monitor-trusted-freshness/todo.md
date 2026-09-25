---
status: draft
work_item: monitor-trusted-freshness
issue: 1078
domain_breadth: 1
state_consistency: 1
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
---

# Monitor trusted freshness（#1078）工作計畫

## Binding

唯一 owner 是 [issue #1078](https://github.com/hamanpaul/paulsha-cortex/issues/1078)，唯一 `work_item` 是 `monitor-trusted-freshness`；Superpowers spec/design、這份 Todo 與 own OpenSpec change `monitor-trusted-freshness` 均使用此 binding。父 umbrella #1064 的 `monitor-correlation-refresh-generation` mapping 不共用、不轉移。

## Boundary and dependency

只交付 exact `.cortex/work-items.yaml` input capture、同代 provider/source revision manifest、durable `WorkSnapshot` read-back、same-generation success publication 與一個唯讀 Monitor freshness API。硬依賴 #1077 的 generation/marker store 與窄 success-transition contract，並沿用 #1063 的 source/path qualification contract。#1077 尚未交付前不得實作 ledger writer 或另建 allocator/store。#1065 owns WorkAuthority consumption；#1054 owns Manager admission；兩者及 CLI、recovery、ship 均不在本票。

## Sizing and gate status

- 官方 sizing 使用 `paulsha_cortex.coordinator.work_bridge.current_sizing_snapshot()`、`fix-standard` 與本 Todo、同一 `work_item` 的 Superpowers spec/design 三列；OpenSpec capability delta 綁在 own change，不另計 helper row。
- 目前三份 sizing rows 都是 `draft`；官方 current draft 為 **8/10 Red**：`domain_breadth=1`、`state_consistency=1`、`acceptance_surfaces=2`、`spec_stability=2`、`orchestration=2`；completeness missing kinds 為 spec/design/plan。
- Accepted-status projection 僅在隔離暫存副本把三份 sizing rows 的 `status` 改為 `accepted`，正文及其他 metadata 不變；官方 completeness 為 complete、missing kinds 為空，counterfactual 為 **6/10 Yellow**（1+1+2+0+2）。這是條件投影，不表示目前接受、intake 或產品實作授權。
- 順序固定為 #1063 → #1077 → #1078 → #1064 → #1065 → #1054。本 planning PR 不執行 intake。

## Current implementation trace

- `paulsha_cortex/monitor/work_api.py` 的 `WorkModelRefresher.refresh()` 負責 provider scan、per-repo correlation/projection 與寫入 snapshot；目前保留 provider last-good 狀態，不能單靠 row 推論最新成功。
- `paulsha_cortex/monitor/correlation.py` 的 `correlate_work_sources()` 透過 `load_work_item_overrides()` 讀取 override，refresh 尚未綁定 parser 實際消費 bytes 的 revision。
- `paulsha_cortex/monitor/work_snapshot.py` 的 `WorkSnapshotStore` 原子持久化 canonical snapshot；`sequence` 和 `written_at` 不能取代 attempt generation 或成功 read-back manifest。
- `WorkModelRefresher.stale_after_seconds` 已由 Monitor config 注入；future API 必須重用此設定，不接受 caller 自訂 age/root。

## Tasks

- [ ] **T1 — Freeze #1077 integration seam**：在開始本票產品實作前，核對 #1077 已交付的 generation marker schema/store，確認其提供 expected-generation 的 `running → succeeded` 窄發布操作；固定此票只能消費該 store，不實作 allocator、running/failure writer 或第二 marker store。若 seam 不足，先在依賴票對齊，不以本票繞過。
- [ ] **T2 — Capture exact correlation input**：讓 refresh 將 `.cortex/work-items.yaml` 一次讀取的 raw bytes revision 與同 bytes 解析結果共同送入 correlation；明確 absent sentinel；讀取、decode、parse error 不得留下成功 manifest。
- [ ] **T3 — Record same-generation source manifest**：按 repo 記錄該次成功 candidate 實際消費的 provider revisions 及完整 `source_id → revision`；要求來源同一 generation/snapshot，拒絕缺失、unknown、degraded、stale provider 和 last-good source 混入。
- [ ] **T4 — Read back before success**：先 durable-write candidate `WorkSnapshot`，reload canonical path，核對 canonical digest、sequence、repo/work rows、source ownership、provider/source revisions；所有檢查吻合後以 T1 seam 對同一 expected generation 原子發布 succeeded 與 evidence，任何差異維持 untrusted。
- [ ] **T5 — Add one freshness API**：以 `(repo, work_id)` 為唯一查詢身份；從 Monitor 最新 `ProjectState` 解析 canonical root，讀取 current input、latest attempt、manifest 與 snapshot；核對 generation、digest/sequence、source/provider set 與 `stale_after_seconds`，回傳 immutable typed verdict 和穩定 reason codes，不做 refresh/write/backfill。
- [ ] **T6 — Isolated tests**：使用 temporary snapshot/marker stores、fake providers 與 fake clock，驗證健康成功綁定、未 refresh 的 input override drift、snapshot read-back drift、marker/snapshot generation 不同代、legacy/unknown/malformed/running/failed marker、degraded/missing source/provider、超齡資料、ambiguous root 與 I/O errors；不讀寫正式 Monitor state、GitHub 或模型。
- [ ] **T7 — Docs and delivery gates**：同步 Monitor API 文件、own OpenSpec tasks 與 changelog fragment/Unreleased；執行 focused/full tests、`openspec validate monitor-trusted-freshness --strict --no-interactive`、repo canonical spec validation、PR-context policy、diff check。只封存本票 own change；不得聲稱 #1064/#1065/#1054 完成。

## Planning delivery status

本計畫仍為 `draft`；本輪產物是 issue-backed planning packet。PR merge、formal Cortex intake、#1077 implementation、Monitor runtime changes、WorkAuthority/Manager consumer、部署及 live state 驗證都不是本 PR 的完成宣告。

---
status: proposed
work_item: sizing-envelope-calibration
---

## Goals

把 `#209` 供給側封套（`accepts_bands`／`invariant_ceiling`／`consistency_scope`／
`acceptance_modes`）從手估校準為可用 cortex 自身 run 歷史計算，並把 `#208` sizing 宣告的
先驗補上後驗（歷史 diff LOC 中位數）。方法參考 `paulsha-patchmud` 的量表設計，僅為方法參考，
不建立跨 repo 依賴。**不實作任一 estimator、不改任何 `.py`、不新增資料源。**

## Why

`#209`（本票唯一依賴）已定案供給側四欄位的契約與落地位置，但四欄位本身尚未落地（main 現況
零命中）；`#208`／`#222` 已留下 `sizing_declaration_drift` 欄位並註解「供 `#210` 後驗」，但
本票查證發現：(1) 該欄位粒度是模組數，與 issue §2.1 patchmud 方法要的 diff LOC 中位數量綱
不同；(2) `invariant_ceiling` estimator 真正需要的 `invariant_count` 歷史值**從未被
`CompletionRecord` 持久化**——`planning.py` 只在 plan-review 當下做一次性比對，不留存。這
兩個落差是本票查證到、issue 原文與現有 workstream 追蹤紀錄都未提及的新發現，必須先在設計層
定案，才能讓後續實作票有明確資料源可用，避免重複踩坑。

## What Changes

- 新增設計文件定案 `calibration_source`／`calibrated_at` 只掛在 `invariant_ceiling`
  （非全部四個 `#209` 欄位），理由是唯獨這個欄位有 issue §3 給出的具體校準演算法。
- 定案難度後驗 estimator 的正確資料源改為 `CompletionRecord.work_authority.merge_commit`
  本地 `git diff --shortstat`，不沿用粒度不符的 `sizing_declaration_drift`。
- **記錄新缺口**：`invariant_ceiling` estimator 所需的 `invariant_count` 歷史值目前完全
  沒有持久化路徑，裁定需要一張獨立前置票補上 `CompletionRecord` 新欄位（暫定
  `plan_invariant_count`），並定案「一次通過率」的操作型定義（排除非 `model_repair` 的
  retry 原因）。
- 定案 estimator 觸發時機比照 `cortex stat` 既有四個彙總旗標（即時查詢，非背景批次）。
- 裁定不採納 issue §2.3 對 `consistency_scope` 的 glob 化建議，維持 `#209` 已凍結的產物
  種類集合契約。
- 定案三張前置票的交付順序（`#209` 欄位落地 → 本票兩張前置票 → 兩個 estimator →
  `cortex stat --calibration`）。
- 不實作、不改 `.py`、不新增資料源。

## Capabilities

### Modified Capabilities
- `persona-workflow-orchestration`：契約 delta 見
  `specs/persona-workflow-orchestration/spec.md`；完整 Requirements 與 Decisions 詳見
  `docs/superpowers/specs/sizing-envelope-calibration-{spec,design}.md`。

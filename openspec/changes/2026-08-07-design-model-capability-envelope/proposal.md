---
status: accepted
work_item: design-model-capability-envelope
---

## Goals

定案 `#138` judge 公式裡「能力配得上」謂詞的 `capable()` 六項判準與 `resource-inventory`
四個靜態欄位契約，並定案 topic×sizing band 路由矩陣現況語意與 `#136`／`#138`／`#209` 三閘
（eligibility／admission／routing）邊界，供 `#138`（judge）與 `#210`（校準）消費。**不實作
`capable()`、不改任何 registry schema、不實作 judge。**

## Why

`#208` 已落地需求側五維 sizing 量表（`claim.py:1098` `sizing_band()` 等，main 生產可用），
但供給側「能力配得上」完全未定義：`claim_readiness.py:18,421-437` 明文標註
`capability` 檢查是 `#209 not yet landed` 的 observability bypass（恆真、無過濾）；
packaged registry（`model-identities.yaml`）現況只有一個身分且無 `build`/`review`
capability；issue #209 §4.1 的三身分表（含其自身 2026-07-27 修正 comment）與 main tracked
現況皆不符（`grep` 全 repo 零命中該表列的 model_id）。issue 本文自稱「設計討論記錄，非實作
PR；落地依 OpenSpec 開 change」，本票即為該 change。

## What Changes

- 新增設計文件定案 `capable()` 六項合取式（逐項標註來源：`sizing_band`←`#208`、
  `track_record`←`#137`、其餘四項←本票）與四個新靜態欄位（`accepts_bands`／
  `invariant_ceiling`／`consistency_scope`／`acceptance_modes`）的型別／值域／複合鍵契約。
- 定案落地位置：短期併入既有 `model-identities.yaml`（schema v2→v3），不新建 issue 原文提及
  但 `#139` 任務清單未涵蓋的 `resource-inventory.yaml`。
- 定案三閘序（eligibility／admission／routing）並記錄與既有 `claim_readiness.CHECK_ORDER`
  的落差；定案 topic×band 矩陣在現行 roster（僅 1 個 `build` 身分）下只有 eligibility 語意、
  無 routing 語意。
- **§4 現況更正**：registry 全文只有一個身分（`agy`/`gemini-3.1-pro-high`/
  `capabilities: [planning]`），連 issue #209 自身修正 comment 的三身分表都對不上 main。
- 明載既有消費端契約點（`planning.py:456-509` 的 `envelope_lookup` 介面形狀）必須被後續
  實作票沿用，不得另起爐灶。
- 不實作、不改 `.py`、不新增資料源。

## Capabilities

### Modified Capabilities
- `persona-workflow-orchestration`：契約 delta 見
  `specs/persona-workflow-orchestration/spec.md`；完整 Requirements 與 Decisions 詳見
  `docs/superpowers/specs/design-model-capability-envelope-spec.md` 與
  `docs/superpowers/specs/design-model-capability-envelope-design.md`。

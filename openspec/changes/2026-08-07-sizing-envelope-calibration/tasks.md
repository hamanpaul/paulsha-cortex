---
status: proposed
work_item: sizing-envelope-calibration
---

# Tasks

- [x] 1.1 新增 `openspec/changes/2026-08-07-sizing-envelope-calibration/`
      （`proposal.md`／`design.md`／本檔／`specs/persona-workflow-orchestration/spec.md`）。
- [x] 1.2 新增 `docs/superpowers/specs/sizing-envelope-calibration-spec.md`
      （Requirements R1–R9，逐條回答 issue §2／§3／§4／§5，並記錄查證到的兩個新缺口：
      diff LOC 粒度落差、`invariant_count` 從未持久化）。
- [x] 1.3 新增 `docs/superpowers/specs/sizing-envelope-calibration-design.md`
      （Decisions D1–D6、交付順序圖與風險緩解，逐項附 main 檔案:行號證據）。
- [x] 1.4 新增 `docs/superpowers/plans/sizing-envelope-calibration.md`（issue §4 四項交付
      逐項標記「已完成」／「待設計」並指向對應 D/R 條目）。
- [x] 1.5 更新 `docs/superpowers/workstreams/cost-governance-cluster/todo.md` 的 `#210`
      條目，指向本次新建的 plan/design doc，並註記已超前完成三項不必重做。
- [x] 1.6 `changelog.d/sizing-envelope-calibration.md` fragment 與
      `CHANGELOG.md [Unreleased]` entry。
- [x] 1.7 `python3 -m pytest -q` 全綠（docs-only，基線不變：2042 passed + 32 subtests）；
      `openspec validate 2026-08-07-sizing-envelope-calibration --strict` 通過。

## 本票不做（範圍切分給後續實作票）

- 不新增 `CompletionRecord.plan_invariant_count`（或等效欄位）。
- 不改 `model-identities.yaml`／`model_identities.py`（`calibration_source`／
  `calibrated_at` 欄位落地）。
- 不實作難度後驗 estimator 或 `invariant_ceiling` estimator。
- 不改 `cli.py`（`cortex stat --calibration` 或等效旗標）。
- 不改 `#209` 已凍結的任何欄位契約（`accepts_bands`／`invariant_ceiling`／
  `consistency_scope`／`acceptance_modes` 的型別／值域／複合鍵）。

## 建議後續實作票切分（皆為未來新 issue，不在本批；依 spec R9 交付順序）

1. **（前置：`#209` 自身）欄位 schema PR**——`model-identities.yaml`／
   `model_identities.py` 升版 schema v3，落地 `#209` 四欄位並補至少一個 `build`
   capability 身分。本票的兩個前置票皆依賴它先完成。
2. **`plan_invariant_count` 持久化 PR**——`CompletionRecord` 新增可選欄位記錄 plan 階段
   宣告的 `invariant_count` 快照，比照 `sizing_declaration_drift` 既有慣例（可選欄位＋
   `_normalize_*`＋extras 白名單聯集）。驗收：既有 `completion.py` 測試全綠＋新欄位型別／
   缺省語意測試。
3. **`calibration_source`／`calibrated_at` 欄位 PR**——`model-identities.yaml` 新增兩欄位
   掛在 `invariant_ceiling` 上（schema 版本號策略由該票自定）。可與票 2 平行進行。
4. **難度後驗 estimator PR**——落地 `merge_commit` 本地 diff → 難度尺度計算（依 D2／R2／
   R6），依賴票 1。驗收：樣本數 0/1/2/3 邊界行為測試（未校準 fail-soft）。
5. **`invariant_ceiling` estimator PR**——落地通過率-vs-`invariant_count` 曲線衰減點計算
   （依 D3／D4／R3／R4／R6），依賴票 2、3。驗收：`retry_classification` 過濾邏輯測試、
   樣本不足 fail-soft 測試。
6. **`cortex stat --calibration` PR**——落地顯示介面（依 D5／R5／R8），依賴票 4 或票 5
   至少一個完成。驗收：輸出格式比照既有四個彙總旗標的包裹慣例。

## 驗收（本票）

R1–R9 逐條可對照回 issue #210 原文對應段落；`git diff` 只涉及 `docs/**`、`openspec/**`、
`changelog.d/**`、`CHANGELOG.md`，未觸碰任何 `.py`／`.yaml`；`grep -rn
"calibration_source\|calibrated_at\|plan_invariant_count" paulsha_cortex/` 只命中本次
新增的設計文件（若被 grep 到 `.py`／`.yaml` 即代表本票範圍外洩，需修正）。

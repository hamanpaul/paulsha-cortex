---
status: proposed
work_item: sizing-envelope-calibration
---

## Decisions

- `calibration_source`／`calibrated_at` 定案只掛在 `invariant_ceiling`
  （`(executor, model_id)` 複合鍵，沿用 `#209` R2 掛載鍵），不對 `accepts_bands`／
  `consistency_scope`／`acceptance_modes` 各自附一份——後三者是 operator 宣告，不是可從
  歷史觀測值統計出來的量，唯獨 `invariant_ceiling` 有 issue §3 給出的具體校準演算法。
- 難度後驗 estimator 的資料源定案為 `CompletionRecord.work_authority.merge_commit` 本地
  `git diff --shortstat`，不是 `sizing_declaration_drift`（後者粒度是模組數，與 patchmud
  diff LOC 方法量綱不同，本票查證發現此落差，issue 原文與既有 workstream 追蹤紀錄皆未
  提及）。
- **記錄新缺口**：`invariant_ceiling` estimator 需要的 `invariant_count` 歷史值目前完全
  沒有持久化路徑（只存在於 `planning.py` plan-review 當下一次性比對）。裁定需要一張獨立
  前置票新增 `CompletionRecord` 可選欄位（暫定 `plan_invariant_count`，比照
  `sizing_declaration_drift` 既有慣例），本票不凍結最終欄位名，只定案「需要補」與「比照
  哪個既有慣例」。
- 「一次通過率」定案為 `retry_classification` 缺席或 `!= "model_repair"` 的
  `CompletionRecord` 佔比，排除編排層／環境層原因造成的 retry，避免系統性低估
  `invariant_ceiling`。
- estimator 觸發時機定案比照 `cortex stat` 既有四個彙總旗標（即時查詢、現場計算），不落地
  背景批次或另一份快取檔案。
- `consistency_scope` 維持 `#209` 已凍結的產物種類集合語意，不採納 issue §2.3 對
  builder `write_paths` 的 glob 比對建議——`#209` 已進入 `accepted` 狀態，本票無權片面
  推翻，若該 glob 機制仍有必要應另開獨立票。
- 交付順序：`#209` 欄位 schema PR 先落地 → 本票兩張前置票（`plan_invariant_count` 欄位／
  `calibration_source`+`calibrated_at` 欄位，互不相依可平行）→ 兩個 estimator → `cortex
  stat --calibration`（至少一個 estimator 可用即可先行串接）。

詳細 D1–D6 與風險緩解見
`docs/superpowers/specs/sizing-envelope-calibration-design.md`。

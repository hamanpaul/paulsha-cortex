### Added
- **Issue #137：交付 one-shot 成效閉環（lesson-loop + 棘輪計分）設計文件**：新增
  `docs/superpowers/specs/oneshot-lesson-loop-{spec,design}.md`。凍結 `task_type ×
  outcome` 計分 schema——計分鍵沿用 `#139` taxonomy loader 取得的 `(type, scope)`
  tuple、`outcome` 為 `clean`／`fixup`／`fail` 三態並給出機械推導規則（`delivery.
  ReviewLoop.fix_rounds`）、`cost` 為 reserved／nullable 且明定其未來由 `#325` 已落地
  的 `usage_aggregate.aggregate_usage_by_run()` 投影而來；定案 session-health 為診斷
  特徵而非 reward 成分的語意邊界；定案 lesson 萃取的 cortex 端輸出介面契約——只產出
  payload，MUST NOT import 或操作 `paulsha-hippo` 的 `knowledge/` 目錄，recall 完全屬
  hippo range；定案棘輪 `track_record(resource, task_type, scope=None) -> float` 介面
  契約，與 `#209` 已凍結的 `capable()` 第六項簽章相容，並建議掛點為 `#209 capable()`
  的判準之一（供 `#138` judge 消費）而非另開 `autonomy.py` 內部路徑。Decisions 段落另
  查證 `#275` engineering-outcome outbox 目前未捕捉 `fix_rounds`，建議未來實作票在
  `_ship_action`／`_abandon_action` 既有 `emit_outcome()` 呼叫點 additive 補上該欄位，
  讓 track_record 可作為該 outbox 的下游 reducer 而非另開一條終局捕捉路徑。本票不實作
  `track_record.py`、不動 hippo、不開 `openspec/changes/**`（依 issue 原文，落地時再
  依 OpenSpec 流程另開）。同步更新
  `docs/superpowers/workstreams/cost-governance-cluster/todo.md` 的 `#137` 列狀態。

---
status: accepted
work_item: feat-slice-executor-model
---

# feat-slice-executor-model Design

## Decisions

- 採 issue 期望選項 1：deck slice frontmatter 擴充 optional per-slice `executor`／`model_id`
  成對宣告，fanout 層旗標降為預設；不採多 specs-dir root（只解依賴圖分裂、不解表達力）。
- 語法驗證（成對、非空）在 parse 層（`_normalize_frontmatter`），registry 存在性驗證在
  dispatch 層（per-slice）；unknown identity fail-closed 帶可用清單（語意比照 #205 D4）。
- per-slice launcher 經 launcher_factory 注入，形狀沿用 workflow 既有 `_resolve_launcher`；
  `as_commit_required` 與 `allow_unsafe` 語意一體適用、不新增旁路。
- depends_on 批外診斷三分類：`deps-unsatisfied`（in-batch）／`deps-external`（handoff
  manifest 存在）／`deps-unknown`（無 trace）；只做可觀測診斷不升級 hard gate；
  `detect_cycles` 外部邊不算環的判定不動。
- fanout／tick／dispatch request 只在 executor 與 model 皆明確時查 model-identities
  registry（#276 同場發現收口）；model None 維持現行為。
- `EMITTED_FRONTMATTER_FIELDS` 同步擴充維持 deck contract 雙向等式；deck compile 產物
  維持 identity-agnostic（不輸出新欄）。

詳細 D1–D6 與風險緩解見 `docs/superpowers/specs/feat-slice-executor-model-design.md`。

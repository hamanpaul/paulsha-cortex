---
status: accepted
work_item: feat-slice-executor-model
---

## Goals

spec frontmatter 支援 optional per-slice `executor`/`model_id` 成對宣告（宣告值經 model-identities registry fail-closed 驗證，未宣告沿用 fanout 層預設），讓單一 specs-dir 容納異質 executor、dependency graph 不再被迫分裂；同時為批外 `depends_on` 提供 `deps-external`/`deps-unknown` 顯式分類診斷消除 typo fail-silent，並收口 fanout/tick request 層 builder identity 不經 registry 驗證的缺口（#294；#276 同場發現）。

## Why

executor/model 目前只能在 fanout 層指定（`cortex fanout --executor X --model Y`），spec frontmatter 宣告 `executor` 即 `unknown frontmatter key`。一個 plan 需要「不同 slice 用不同 executor/model」時只能切多個 `--specs-dir` 分開 fanout，依賴圖隨之分裂；而 `depends_on` 指向批外 slice_id 時，`detect_cycles` 跳過、`ready_units` 只問滿足性，「合法跨 dir 依賴（未完成）」與「打錯字的不存在 slice_id」可觀測行為 100% 相同——held reasons 一律 `deps-unsatisfied`，typo 讓 slice 靜默永不派工（#294 於 IntelliDbgKit 實測重現）。另 workflow 路徑的 identity 一律經 model-identities registry 驗證（#205 override 亦 fail-closed 列可用清單），但 fanout/tick/dispatch request 與 periodic tick 的 `--model` 原樣進 executor argv、不查 registry，typo 要到 session 內才失敗（燒 session）。

## What Changes

- spec frontmatter 新增 optional `executor`/`model_id`：成對宣告（單獨其一 → `invalid-frontmatter`）、非空字串；`_normalize_frontmatter` allowed set 與 `deck/schema.py` 的 `EMITTED_FRONTMATTER_FIELDS` 同步擴充；deck compile 不輸出新欄；未宣告時 meta 帶 None、行為位元不變。
- `dispatch_ready` 新增 optional `identity_registry`/`launcher_factory`：宣告 identity 經 `load_model_identities()` 驗證，unknown fail-closed（不建 worktree、不啟 model session、slice 標 `needs_human`、錯誤列可用 identity 清單），單 slice 失敗不波及同批；通過則以 factory 建 per-slice launcher（`as_commit_required`／`allow_unsafe` 語意一體適用），覆寫值進 job dispatch argv 與 job row `executor`/`model_id`。
- 批外 `depends_on` 顯式分類：有 handoff manifest → `deps-external:<id>`、無任何 trace → `deps-unknown:<id>`、in-batch 未完成維持 `deps-unsatisfied:<id>`；`cortex status` held reasons 呈現分類，`cortex ready` 對 unknown 印 stderr 診斷；`detect_cycles`/`ready_units` 不因批外 dep 拒絕整批（合法跨 dir 時序保留）。
- fanout/tick/dispatch request 與 periodic tick：明確 `(executor, model)` 對於派工前經 registry 驗證，unknown fail-closed 帶可用清單；model 未指定維持現行為。
- CLI help（`cortex fanout`/`tick`/`run fanout` 的 `--model`）、README fanout/tick 與 model-identities 段、auto dispatch 契約文件同步。

## Capabilities

### Modified Capabilities

- `trusted-dispatch-completion`：詳見 `docs/superpowers/specs/feat-slice-executor-model-spec.md` 的 Requirements（R1–R5）與 `docs/superpowers/specs/feat-slice-executor-model-design.md` 的 Decisions（D1–D6）。

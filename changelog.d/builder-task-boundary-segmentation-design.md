### Added
- **Issue #276：builder 派工依 plan Task 邊界分段——設計文件（design-doc）**：新增
  `openspec/changes/2026-08-07-builder-task-boundary-segmentation/`（proposal／
  design／tasks／`specs/trusted-dispatch-completion/spec.md`）與
  `docs/superpowers/specs/builder-task-boundary-segmentation-{design,spec}.md`，
  定案 D1-D6：per-Task fan-out（新增同 worktree 續派原語，不重用會與既有
  worktree 衝突的 `Dispatcher.dispatch()`）、Task 邊界解析（新函式
  `planning.list_plan_tasks()`，不重用回傳值攤平的既有 `_collect_task_items()`）、
  `build_dispatch_prompt()` 的 optional `task_slice` 參數與反漫遊／commit
  斷點語句（未傳時逐位元向後相容）、`classify_completion()` 新增
  `context-exhausted` 分類（偵測 `ran out of room in the model's context
  window` 字串）、commit log 作續跑進度帳（不採 plan checkbox 回寫）、
  與 #277（completion 快照競態）的介面邊界。本票不動任何
  `paulsha_cortex/` 程式檔；code 落地拆為三張後續票（見 `tasks.md` 文末
  拆票建議）。

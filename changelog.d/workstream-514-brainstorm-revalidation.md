# workstream-514-brainstorm-revalidation

- **`#514` workstream 佈線**——新增 `docs/superpowers/workstreams/fix-brainstorm-revalidation-diagnostics/todo.md`，
  為 `#514`（`_validated_brainstorm_planning_authority()` 對已持久化 artifact 的重驗失敗，只 raise 不含
  `ref` 與 `assessment.reasons` 的例外）建立 work item 的 **todo 來源**。cortex 的 lifecycle
  （`monitor/lifecycle.py:reduce_lifecycle`）需要 `active_todo` 才會把 work item 由 `topic` 推進到
  `todo`；缺此來源時 `cortex work start` 會以 `authority-not-startable` 拒絕、auto-claim 亦不受理。
  內容記錄根因、四項驗收任務，以及它與 `#511`／PR `#513` 的關係（同類缺陷、不同觸發時機：首次寫入
  vs 已持久化 artifact 的重驗）。

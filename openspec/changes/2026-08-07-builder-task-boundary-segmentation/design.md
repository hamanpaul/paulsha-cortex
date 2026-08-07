---
status: draft
work_item: builder-task-boundary-segmentation
---

# builder-task-boundary-segmentation Design

## Decisions

- D1 分段執行模型：manager tick 迴圈（`manager_daemon.py:
  build_periodic_tick_runner`）對同一 slice 偵測前段終局後反覆呼叫
  `Dispatcher` 新增的同 worktree 續派方法；不重用會重建 worktree 而衝突的
  `Dispatcher.dispatch()`，不在 `Dispatcher` 內部自跑迴圈（迴圈節奏需對齊
  既有 tick backoff／限流）。
- D2 Task 邊界解析：新增 `planning.list_plan_tasks()` 回傳按 `## Task N`
  分段的 `TaskUnit` 序列；既有 `_collect_task_items()`（攤平、僅供 plan
  review 完整性檢查）不動、不重用於派工路徑。
- D3 prompt 模板：`build_dispatch_prompt()` 新增 optional `task_slice`
  參數，未傳維持現行整份 plan 行為位元不變；傳入時嵌反漫遊紀律＋段尾
  commit 斷點語句，取代整份 plan 引用。
- D4 completion 分類：`classify_completion()` 新增 `context-exhausted`
  第三態，優先權高於既有 `exited`／`failed`；貫穿 dispatcher／manager
  recovery 邏輯，區分「有部分 commit 可續跑」與「零 commit 需人工判斷」。
- D5 續跑帳本：採 commit log／`dispatch_head` baseline 前進作完成游標，
  持久化於 job／slice 層；不採 plan checkbox 回寫（避免額外 commit 時序
  競態）。
- D6 與 #277 邊界：D4 分類輸出是 #277（completion 快照競態）recovery
  邏輯的輸入之一，兩票不得各自實作互相打架的獨立判斷路徑；本票只記交會
  點，不實作 #277。

詳細 D1–D6 論證、file:line 錨點與風險緩解見
`docs/superpowers/specs/builder-task-boundary-segmentation-design.md`。

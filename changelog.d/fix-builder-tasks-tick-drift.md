### Fixed
- **Issue #296：builder tick tasks.md 與 reviewer authority-proving 凍結
  baseline 矛盾——確認已由 #310 修復，補production-fidelity 迴歸測試**：
  #296 與 #310 描述同一起 2026-08-04 hippo 事故，各自獨立提報；#310
  的修法（PR #311／#312，`_workflow_input_snapshot` 與
  `_authority_map_with_checkbox_tolerance` 對 kind=plan 的
  `tasks.md`／`todo.md` 做 checkbox-insensitive 容忍）在 #296 提報後數小時
  即落地，但 #296 本身從未被關閉核實。新增
  `tests/test_builder_tasks_tick_verify_dispatch.py`，以真實 git repo（而非
  單元層級 fixture）重現 `_dispatch_workflow_card` 的 reviewer 分支
  （verify／review 兩個 phase 共用）：(a) 僅切換 checkbox 通過、(b) tasks.md
  文字被改動仍擋下、(c) proposal.md 等 spec 檔被改動即使伴隨合法 checkbox
  tick 仍擋下；並以還原至 #310 修法前的 manager.py 反向驗證這些測試在缺修法
  時確實會炸，證實非空判。無需再改動 production code。

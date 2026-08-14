# define-resume-coverage

- **`#536`（最小修）：tick resume 迴圈納入 define 階段的 ongoing run**——實測 run
  `workflow-7a430d31eff66ef13630` 停在 define/ongoing/facets 空（brainstorm 已把 spec/design
  發佈到 operator worktree，但 run 狀態未推進——發佈與狀態更新非同一事務），而
  `manager_daemon` 的 resume 迴圈 phase filter 排除 `define`，使這種 run **對所有恢復機制
  永久隱形**：無 facet 可呈現、`next_actions` 空、無任何 tick 會再碰它。
  `resume_workflow_run` 本身完整支援 define（先 reconcile planning publication transaction、
  再 dispatch planner 卡），排除毫無必要。#373 的 needs_human 縱深防禦守衛不受影響
  （測試釘住）。`#536` 其餘項目（發佈／狀態同一事務、phase 心跳）留待 R0.5 D5 與
  attestation 狀態機。

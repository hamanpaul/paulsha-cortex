### Fixed

- **plan-phase planner 卡可在產物完備時由 Manager 決定性通過**：`writing-plans` 等規劃卡若其 persisted planning authority 對應的 accepted spec/design/plan 已完整存在，manager 會直接把當前 planner step 標記為 `passed` 並推進到下一 phase，不再多派 planner executor。

### Added

- **#1226 planning 的 quota fallback**：enforce 下在 brainstorm 開始前做一次唯讀的可行性選組，這也是同一 attempt 的邊界。primary 不可行時，從已探測可用的 planning identity 中改選一個，前提是仍挑得到 domain 不同、而且可行的 secondary；`select_secondary_planner` 新增選填的 `admissible` 過濾。整組都不可行時 wait，不呼叫任何模型。admit receipt 會記下被排除的候選，define step 記錄實際使用的 primary。shadow 模式不改變選擇（#1226）。

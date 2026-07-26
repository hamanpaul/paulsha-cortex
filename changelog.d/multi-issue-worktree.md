### Fixed

- **Issue #134：multi-issue build 不再限制只允許單一 issue**：Builder 在 build phase 會採用排序後最小的 issue 編號作為 `feature/<issue>-<work_id>` 的主 branch，並改用 run workspace repo 作為 worktree 建立來源。

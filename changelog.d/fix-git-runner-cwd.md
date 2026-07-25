### Fixed

- **Issue #99：git runner 與 service units 使用 repo root 定位**：`_default_git_runner` 改為以 `paths.repo_root()` 呼叫 `git -C <repo_root>`，並將 `cortex-manager.service` 與 `cortex-monitor.service` 渲染加入 `WorkingDirectory=<repo_root>`，避免系統d 啟動目錄漂移。

### Added

- **porcelain-bootstrap 單步上手 CLI**：新增 `cortex bootstrap`，把 preflight、`service install/start`、`inspect status/doctor` 摘要、`--dry-run` 與非阻斷 `--sample` 串成單一 `cortex-porcelain/bootstrap/v1` 入口。

### Fixed

- **porcelain-bootstrap executor preflight**：`bootstrap` preflight 改為檢查實際 executor 登入態（`copilot` / `claude` / `codex`），並移除未列入凍結設計的額外 `gh-auth` gate。

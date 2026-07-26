### Fixed

- **Issue #101：deck emit frontmatter 補齊 auto dispatch 合約**：`paulsha_cortex/deck/compile.py` 讓 `--emit` 產生 frontmatter 時帶入非空 `target_branch` 並補齊 `verification` skeleton（含 `persona-scope`、`name=policy` command、`tests` 與 `full_suite.baseline=no-regression`）。

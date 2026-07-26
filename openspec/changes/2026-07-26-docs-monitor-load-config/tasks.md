---
status: accepted
work_item: docs-monitor-load-config
---

# Tasks

- [x] [RED] `tests/test_monitor_config_explicit.py`：`load_config(config_path=<explicit>)` 不合併 ambient hippo projects（即使環境中有 hippo project 存在）；`load_config()` 不帶 config_path 時仍合併 ambient。
- [x] [實作] `docs/`（如 `docs/monitor-config.md` 或既有 monitor 文件）：記錄 `load_config` 的 explicit vs ambient 行為——顯式傳入 config_path 時 fully explicit。
- [x] [同步與驗證] `changelog.d/docs-monitor-load-config.md` fragment；`CHANGELOG.md [Unreleased]` `### Documentation` 加入含 `#143` 條目。
- [x] [同步與驗證] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [x] [同步與驗證] 勾選本 tasks.md 對應項並以 conventional commit 提交。

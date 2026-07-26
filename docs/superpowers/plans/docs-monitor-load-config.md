---
status: accepted
work_item: docs-monitor-load-config
---

# docs-monitor-load-config Plan

## Tasks

### 1. TDD RED

- [ ] `tests/test_monitor_config_explicit.py`：
  - `load_config(config_path=<explicit>)` 不合併 ambient hippo projects。
  - `load_config()` 不帶 config_path 時仍合併 ambient。
  - 先確認 RED。

### 2. 實作（docs-only）

- [ ] `docs/`：新增或更新 monitor 文件，記錄 `load_config` 的 explicit/ambient 合併語意。

### 3. 同步與驗證

- [ ] `changelog.d/docs-monitor-load-config.md` fragment；`CHANGELOG.md [Unreleased]` `### Documentation` 加入含 `#143` 條目。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 勾選 `openspec/changes/2026-07-26-docs-monitor-load-config/tasks.md` 對應項並以 conventional commit 提交。
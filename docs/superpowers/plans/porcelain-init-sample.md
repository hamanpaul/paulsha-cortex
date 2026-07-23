---
status: accepted
work_item: porcelain-init-sample
---

# porcelain-init-sample Plan

## Tasks

### 1. TDD RED

- [ ] 新增 `tests/test_porcelain_init_sample.py`（mock `deck compile --emit`）：hold 強制、必補欄位清單輸出內容、`deck verify` 提示文案、未知 `--combo` exit 2、`--json` schema `cortex-porcelain/init-sample/v1`；先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/porcelain/init_sample.py`：`init-sample` 命令，包裝 `deck compile --emit`，疊加必補欄位清單與下一步指引。
- [ ] `paulsha_cortex/porcelain/__init__.py`：`_FAMILY_MODULES` 加入 init_sample 模組（註冊表 command name 用連字號 `init-sample`，僅此一行）。

### 3. 同步與驗證

- [ ] README 的 CLI 命令面補 `init-sample` 一段（R-16）。
- [ ] 新增 `changelog.d/porcelain-init-sample.md` fragment，`CHANGELOG.md [Unreleased]` `### Added` 加入含 `porcelain-init-sample` 字樣的條目。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 完成後勾選 `openspec/changes/porcelain-init-sample/tasks.md` 對應項並以 conventional commit 提交（不得改動本 plan 檔）。

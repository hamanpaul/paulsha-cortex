---
status: accepted
work_item: fix-git-runner-cwd
---

# fix-git-runner-cwd Plan

## Tasks

### 1. TDD RED

- [ ] `tests/test_fix_git_runner_cwd.py`：
  - `_default_git_runner(["rev-parse","--show-toplevel"])` 以 `subprocess.run` 被呼叫，斷言 argv 前段為 `["git","-C",<repo_root>]`（mock `subprocess.run`；monkeypatch `paulsha_cortex.config.paths.repo_root` 回 `tmp_path`）。
  - git 失敗時 raise `RuntimeError` 含 `-C` 與 stderr。
  - installer render 的 `cortex-manager.service` 內容含 `WorkingDirectory=`（含 monitor unit）。
  - 先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/coordinator/dispatcher.py`：`_default_git_runner` 改 `git -C paths.repo_root()`，失敗訊息含 `-C`。
- [ ] installer 模板：`cortex-manager.service` 與 `cortex-monitor.service` render 加 `WorkingDirectory=<repo_root>`。

### 3. 同步與驗證

- [ ] `changelog.d/fix-git-runner-cwd.md` fragment；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#99` 字樣條目（git runner cwd 耦合）。
- [ ] README 對應段同步（R-18：git runner cwd 無關、installer WorkingDirectory）。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 勾選 `openspec/changes/2026-07-25-fix-git-runner-cwd/tasks.md` 對應項並以 conventional commit 提交（不得改動本 plan 檔）。
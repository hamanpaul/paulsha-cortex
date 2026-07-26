---
status: accepted
work_item: fix-slice-failed-deadend
---

# fix-slice-failed-deadend Plan

## Tasks

### 1. TDD RED

- [ ] `tests/test_fix_slice_failed_deadend.py`：
  - failed slice 可 transition 至 `needs_human`（不再 raise `ValueError`）。
  - failed slice `actions` 非空。
  - registry daemon 在 `jobs.json` 外部修改後可重載。
  - 先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/coordinator/registry.py`：新增 `failed → needs_human` transition（或 `reset` action）。
- [ ] `paulsha_cortex/coordinator/registry.py`：registry daemon 磁碟重載支援（mtime 比較或 reload 命令）。

### 3. 同步與驗證

- [ ] `changelog.d/fix-slice-failed-deadend.md` fragment；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#153` 條目。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 勾選 `openspec/changes/2026-07-26-fix-slice-failed-deadend/tasks.md` 對應項並以 conventional commit 提交。
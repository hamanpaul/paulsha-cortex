---
status: accepted
work_item: fix-builder-write-paths
---

# fix-builder-write-paths Plan

## Tasks

### 1. TDD RED

- [ ] `tests/test_fix_builder_write_paths.py`：
  - 跨 repo dispatch（target repo 非 `paulsha_cortex`）時 builder `write_paths` 涵蓋目標 repo 路徑，builder 不拒絕寫入。
  - cortex repo 內 dispatch 時行為不變。
  - 先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/persona/personas.yaml`：builder `write_paths` 改為 `["**"]` 或標記 dynamic。
- [ ] `paulsha_cortex/persona/contract.py` 或 `render.py`：render 時動態推導 `write_paths`（若需更精確可從 `run.workspace_root`）。

### 3. 同步與驗證

- [ ] `changelog.d/fix-builder-write-paths.md` fragment；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#118` 條目。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 勾選 `openspec/changes/2026-07-26-fix-builder-write-paths/tasks.md` 對應項並以 conventional commit 提交。
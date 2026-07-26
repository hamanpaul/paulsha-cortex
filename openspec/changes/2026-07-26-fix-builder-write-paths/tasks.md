---
status: accepted
work_item: fix-builder-write-paths
---

# Tasks

- [ ] [RED] `tests/test_fix_builder_write_paths.py`：跨 repo dispatch 時（target repo 非 `paulsha_cortex`），builder persona 的 `write_paths` 涵蓋目標 repo 路徑（如 `**` 或目標 repo 結構推導的路徑），builder 不拒絕寫入。
- [ ] [RED] 既有 cortex repo 內 dispatch 行為不變（`write_paths` 仍涵蓋 cortex 路徑）。
- [ ] [實作] `paulsha_cortex/persona/personas.yaml`：builder `write_paths` 改為動態 pattern（如 `["**"]` 由 worktree boundary 限制）。
- [ ] [實作] `paulsha_cortex/persona/contract.py` 或 `render.py`：render 時動態推導 `write_paths`（從 `run.workspace_root` 或目標 repo 結構）。
- [ ] [同步與驗證] `changelog.d/fix-builder-write-paths.md` fragment；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#118` 條目。
- [ ] [同步與驗證] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] [同步與驗證] 勾選本 tasks.md 對應項並以 conventional commit 提交。
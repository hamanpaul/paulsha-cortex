---
status: accepted
work_item: fix-slice-failed-deadend
---

# Tasks

- [x] [RED] `tests/test_fix_slice_failed_deadend.py`：failed slice 可透過 `retry-build` 或 `reset` action 恢復（transition 至 `needs_human` 或 `building`），不再 raise `ValueError`。
- [x] [RED] failed slice 的 `actions` 不再為空（含恢復 action）。
- [x] [RED] registry daemon 在 `jobs.json` 被外部修改後可從磁碟重載（記憶體狀態與磁碟同步）。
- [x] [實作] `paulsha_cortex/coordinator/registry.py`：新增 `failed → needs_human` transition（或 `reset` action），使 failed slice 可恢復。
- [x] [實作] `paulsha_cortex/coordinator/registry.py`：registry daemon 支援從磁碟重載 jobs（偵測 `jobs.json` 修改時間或提供 reload 命令）。
- [x] [同步與驗證] `changelog.d/fix-slice-failed-deadend.md` fragment；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#153` 條目。
- [x] [同步與驗證] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [x] [同步與驗證] 勾選本 tasks.md 對應項並以 conventional commit 提交。

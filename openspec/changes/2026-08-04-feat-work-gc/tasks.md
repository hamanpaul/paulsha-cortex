---
status: accepted
work_item: feat-work-gc-v2
---

# Tasks

- [x] 1.1 RED：依 `docs/superpowers/plans/feat-work-gc-v2.md` 的 TDD RED 章節新增 `tests/test_work_gc.py`，確認失敗。
- [x] 1.2 實作至 GREEN，範圍限於 `docs/superpowers/specs/feat-work-gc-v2-spec.md` 的 Requirements（含 `_WORK_HELP` 同步與 `tests/test_cli_help_alignment.py` 斷言）。
- [x] 1.3 `changelog.d/feat-work-gc-v2.md` fragment 與 `CHANGELOG.md [Unreleased]` entry（#178）。（檔名依分支 slug 用 `-v2`，與本節原述的 `feat-work-gc.md` 不同，詳見實作回報）
- [x] 1.4 `python3 -m pytest tests/ -q` 全綠；帶 PR 上下文的 `policy_check` 0 fail；`git diff --check` 乾淨。

## 驗收

squash-merge 分支正確判 merged 並可回收；unmerged 分支絕不進 `--apply` 清單；dirty worktree 與 closed-unmerged PR 分支保留並附理由；預設 dry-run 對 worktree／分支／`jobs.json` 零變更；報告逐項含 action 與 reason code。

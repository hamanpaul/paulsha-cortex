---
status: accepted
work_item: reclaim-pr-inheritance
---

# reclaim-pr-inheritance Plan

## Tasks

### 1. TDD RED

- [ ] `tests/test_reclaim_pr_inheritance.py`：
  - `test_new_claim_run_starts_with_empty_pr_refs`：`authority.mapped_prs=(42,)` 時，`_claim_workflow_run` 建立的 run `pr_refs == ()`（needs_human 路徑與 start 路徑皆驗）。
  - `test_terminal_provider_skips_closed_unmerged_pr_closing_links`：餵入含 OPEN / CLOSED-unmerged / MERGED PR 的 terminal 觀測，斷言 `closing_links` 只含 OPEN 與 MERGED 的 PR→issue 連結，不含 closed-unmerged。
  - `test_terminal_provider_keeps_open_pr_closing_links`：OPEN PR 仍產 link（回歸）。
  - 先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/coordinator/work_bridge.py`：`_claim_workflow_run` needs_human 建立路徑（`pr_refs=tuple(...)`）改 `pr_refs=()`；start 路徑 args `pr_refs` 改 `[]`。
- [ ] `paulsha_cortex/monitor/providers.py` `GitHubTerminalProvider._scan`：遍歷 `pull_nodes` 時，`state == "CLOSED"` 的 PR 跳過 `closing_links`（OPEN/MERGED 不變）。

### 3. 同步與驗證

- [ ] `changelog.d/reclaim-pr-inheritance.md`；`CHANGELOG.md [Unreleased] ### Fixed` 加入含 `#175` 條目。
- [ ] README 對應段同步（R-18）若有 re-claim/delivery 說明段。
- [ ] `python3 -m pytest tests/ -q` 全綠；`policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 勾選 `openspec/changes/2026-07-26-reclaim-pr-inheritance/tasks.md`。
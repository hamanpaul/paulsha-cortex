---
status: accepted
work_item: multi-issue-worktree
---

# multi-issue-worktree Plan

## Tasks

### 1. TDD RED

- [ ] `tests/test_multi_issue_worktree.py`：
  - `test_build_branch_uses_canonical_primary_issue_for_multi_issue_run`：2-issue run（如 `repo#34`、`repo#39`）進 build phase 不 raise；注入 fake `WorktreeCreator`（記錄 `create(branch)` 的 branch），斷言 branch == `feature/34-{work_id}`（編號最小為 primary）。
  - `test_single_issue_build_branch_unchanged`：1-issue run branch == `feature/{issue}-{work_id}`（回歸鎖定）。
  - `test_build_worktree_uses_run_workspace_root_not_manager_repo`：run.workspace_root 指向 run repo，Manager repo 不同；斷言 build 構造的 `ScriptWorktreeCreator` 以 run.workspace_root 為 repo（用真實 git repo fixture，驗 `git -C` 作用對象 / worktree pool 為 run repo sibling）。
  - `test_pr_metadata_closes_all_mapped_issues`：multi-issue run 的 `_pr_metadata` body 含 `Closes #34` 與 `Closes #39`（既有邏輯鎖定）。
  - 先確認 RED（多 issue 在現行 `>1` raise 下失敗；worktree repo 測試在現行共用 creator 下指向 Manager repo 失敗）。

### 2. 實作

- [ ] `paulsha_cortex/config/paths.py`：新增 `worktree_root_for(repo: Path) -> Path`（sibling pool，尊重 `PSC_WORKTREE_ROOT`）；`worktree_root()` 改 `return worktree_root_for(repo_root())`，消除重複。
- [ ] `paulsha_cortex/coordinator/manager.py` build phase（~5309-5325）：移除 `len(issue_numbers) > 1` raise；`primary = min(int(n) for n in issue_numbers)`；`builder_branch = f"feature/{primary}-{run.work_id}" if issue_numbers else f"feature/{run.work_id}"`；以 `ScriptWorktreeCreator(repo=run.workspace_root, wt_root=worktree_root_for(Path(run.workspace_root)), base="main")` 取代 `dispatcher._worktree_creator`（僅 build phase、`creator is None` 之分管；保留 `creator is None` raise 的語意改為 run-scoped 構造失敗時 raise）。
- [ ] 匯入：`from ..config.paths import worktree_root_for`（或既有 paths alias）；`from .seams import ScriptWorktreeCreator`（manager.py 已否則補）。

### 3. 同步與驗證

- [ ] `changelog.d/multi-issue-worktree.md`；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#134` 條目（multi-issue Work Item + repo-scoped builder worktree）。
- [ ] README 對應段同步（R-18：multi-issue Work Item 與 repo-scoped worktree）若 README 有 builder/worktree 相關段；無則補一行概念說明。
- [ ] `python3 -m pytest tests/ -q` 全綠；`policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 勾選 `openspec/changes/2026-07-26-multi-issue-worktree/tasks.md` 並以 conventional commit 提交。
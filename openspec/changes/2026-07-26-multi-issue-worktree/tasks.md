---
status: accepted
work_item: multi-issue-worktree
---

# Tasks

- [ ] 1.1 RED：`tests/test_multi_issue_worktree.py` 涵蓋 multi-issue build branch（primary=編號最小）、single-issue 回歸、build worktree 使用 `run.workspace_root`、`_pr_metadata` closes 全部 mapped issues。
- [ ] 1.2 `config/paths.py` `worktree_root_for(repo)` + `worktree_root()` delegate。
- [ ] 1.3 `coordinator/manager.py` build phase：移除 `>1` raise、canonical primary branch、run-scoped `ScriptWorktreeCreator`（#134）。
- [ ] 1.4 `changelog.d/multi-issue-worktree.md` 與 `CHANGELOG.md [Unreleased] ### Fixed`（#134）；README 同步（R-18）。
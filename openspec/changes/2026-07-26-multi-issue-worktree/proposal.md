---
status: accepted
work_item: multi-issue-worktree
---

## Goals

讓 unified workflow builder 支援 multi-issue Work Item（deterministic canonical primary issue branch naming）與 repo-scoped builder worktree（以 `run.workspace_root` 建立，不依賴 Manager 的 `PSC_REPO_ROOT`），消除 #134 的兩個 fail-closed 缺口。

## Why

Monitor/WorkAuthority 已允許多張 issue 收斂成同一 Work Item，但 `manager._dispatch_workflow_card` 在 build phase 對 confirmed issue > 1 拋 `workflow builder requires exactly one confirmed issue`，使 multi-issue run 無法實作與閉合（#179 前置批因此被迫拆成單 issue work item）。同時 builder worktree 透過 dispatcher 共用的 `ScriptWorktreeCreator`（預設 `paths.repo_root()`＝Manager repo），跨 repo run（Manager 在 paulsha-cortex、run 屬 paulsha-hippo）會在錯誤 repo 操作 git 而 fail。

## What Changes

- `coordinator/manager.py` build phase：移除 multi-issue raise；以編號最小 issue 為 primary 構造 `feature/{primary}-{work_id}` branch；構造 run-scoped `ScriptWorktreeCreator(repo=run.workspace_root, wt_root=worktree_root_for(run.workspace_root), base="main")` 取代 dispatcher 共用 creator。
- `config/paths.py`：新增 `worktree_root_for(repo)`；`worktree_root()` 改為其 delegate，消除重複。
- `tests/test_multi_issue_worktree.py`：2-issue claim→build、Manager repo≠run repo worktree、single-issue 回歸、`_pr_metadata` closes all mapped issues。

## Capabilities

### Modified Capabilities
- `coordinator/workflow-build`：multi-issue Work Item 進 build；builder worktree 以 run repo 為錨點。
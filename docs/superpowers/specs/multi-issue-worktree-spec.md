---
status: accepted
work_item: multi-issue-worktree
---

# multi-issue-worktree Specification

#134：Unified workflow builder 對 multi-issue Work Item 與跨 repo run 的兩個 fail-closed 缺口。

## 背景

`_dispatch_workflow_card`（`coordinator/manager.py`）在 build phase 蒐集 `run.issue_refs`，只要 confirmed issue 數量大於 1 就拋 `workflow builder requires exactly one confirmed issue`。Monitor/WorkAuthority 已允許多張 issue 收斂成同一 Work Item（`work_bridge.py` 以 `authority.mapped_issues` 建立含多個 issue_refs 的 run），但 builder 契約不允許同一 run 實作與閉合它們。實證：#179 前置批因本限制被迫將 3-issue `dispatch-reliability-batch` 拆成單 issue work item。

第二缺口：builder worktree 透過 dispatcher 共用的 `ScriptWorktreeCreator`（`coordinator/seams.py`），其 `repo` 預設為 Manager process 的 `paths.repo_root()`（`PSC_REPO_ROOT`）。當全域 Manager 安裝於 paulsha-cortex、run 實際屬於 paulsha-hippo 時，target SHA 與 worktree repo 不一致，跨 repo build 會在 `git -C <manager_repo>` 上下文操作錯誤 repo 而 fail。

## Requirements

### R1 multi-issue run 進入 build

一個 Work Item 可合法綁定一或多張 confirmed GitHub issues。build phase 不得因 confirmed issue 數量大於 1 停止；MUST 改用 deterministic branch naming：

- canonical primary issue = `run.issue_refs` 中編號最小者。
- builder branch = `feature/{primary_issue_number}-{run.work_id}`（與既有單 issue 格式 `feature/{issue}-{work_id}` 逐字一致）。
- 單 issue run：primary 即唯一 issue，branch 與既有行為完全相同（不回歸）。

不再拋 `workflow builder requires exactly one confirmed issue`。

### R2 repo-scoped builder worktree

build phase 的 worktree MUST 以 `run.workspace_root` 對應的 canonical git repo 建立，不得依賴 Manager instance 的固定 `PSC_REPO_ROOT`：

- 為 build phase 構造 run-scoped `ScriptWorktreeCreator`，`repo=run.workspace_root`、`wt_root` 為 run repo 的 sibling worktree pool、`base` 為該 repo 的 default branch。
- 不改動 dispatcher 共用 creator 與 `autonomy.py` 的非 workflow 派工路徑（那裡仍以 Manager repo 為正確 repo）。
- `ScriptWorktreeCreator` 既有 fail-closed 驗證（base `rev-parse --verify`、既有 branch ancestry `merge-base --is-ancestor`、target exists、branch probe）MUST 保留並對 run repo 生效。

### R3 限制

- stdlib-only；TDD（先 RED）。
- 不改對外 CLI `--json` envelope schema 字串。
- delivery PR body 的 closing keywords 涵蓋該 Work Item 全部 mapped issues（`work_bridge._pr_metadata` 已 iter `run.issue_refs`；本批僅以測試鎖定既有行為，不改正式邏輯）。
- `python3 -m pytest tests/ -q` 全綠；`policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- 不處理 #175（re-claim PR 繼承）與 #177（driving-cortex skill）。

## 驗收

- 新增 2-issue Work Item 的 workflow 整合測試：claim → accepted planning → build dispatch 成功（不再 raise `requires exactly one confirmed issue`），且 builder branch = `feature/{min_issue}-{work_id}`。
- 新增「Manager repo ≠ WorkflowRun repo」整合測試：build worktree 建於 run repo 的 sibling pool，`git -C` 指向 run workspace_root 而非 Manager repo。
- 單 issue 既有 branch naming 與 lifecycle tests 不退化。
- multi-issue run 的 `_pr_metadata` 產出含全部 issue 的 `Closes #N`（鎖定測試）。
- CompletionRecord / delivery binding 保留全部 issue source revisions（既有路徑，以測試鎖定）。
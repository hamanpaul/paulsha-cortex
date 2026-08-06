---
status: accepted
work_item: multi-issue-worktree
---

# multi-issue-worktree Design

## Decisions

### D1 canonical primary issue branch naming（非 `feature/<work_id>` 新形狀）

選 **編號最小 issue 為 primary**，branch = `feature/{primary}-{work_id}`，逐字沿用既有單 issue 格式。

不選 `feature/<work_id>`（issue 提到的另一選項）：雖語意合理，但 monitor 的 PR↔issue correlation 有 branch-name heuristic（`feature/{number}-*`，見 `test_monitor_work_providers.py`）；引入無 issue 編號的新形狀會破壞該 heuristic，需連動改 monitor，擴大爆炸半徑。canonical primary 保留既有形狀，monitor 與 delivery 既有邏輯零改動，closing keywords 仍涵蓋全部 issue。

`manager.py:5313-5325` 現行：
```
issue_numbers = [m.group(1) for ref in run.issue_refs if (match := re.fullmatch(rf"{re.escape(run.repo)}#([1-9][0-9]*)", ref))]
if len(issue_numbers) > 1:
    raise ValueError("workflow builder requires exactly one confirmed issue")
builder_branch = f"feature/{issue_numbers[0]}-{run.work_id}" if issue_numbers else f"feature/{run.work_id}"
```
改為：移除 `>1` raise；`primary = min(int(n) for n in issue_numbers)`；`builder_branch = f"feature/{primary}-{run.work_id}"`（issue 為空時 fallback `feature/{run.work_id}` 不變）。單 issue：primary 即唯一 → 行為不變。

### D2 run-scoped worktree creator（build phase 專用）

在 build phase 分支（`manager.py:5309-5325`）不再取 `dispatcher._worktree_creator`，改構造 `ScriptWorktreeCreator(repo=run.workspace_root, wt_root=worktree_pool_for(run.workspace_root), base=DEFAULT_BRANCH)`。保留 `creator.create(builder_branch)` 呼叫介面不變。

- `worktree_pool_for(repo)`：新增 `paths.worktree_root_for(repo: Path) -> Path`，鏡射 `worktree_root()` 的 sibling 邏輯但以傳入 repo 為錨點（`_canonical_repo_root(repo).parent / f"{name}-worktrees"`），尊重 `PSC_WORKTREE_ROOT` env（與既有 `worktree_root()` 同一支函式重用：將 `worktree_root()` 改為 `return worktree_root_for(repo_root())`，避免重複邏輯）。
- `DEFAULT_BRANCH`：run 的 default branch。現行 `ScriptWorktreeCreator(base="main")` 預設 main；本批保留 `main` 為 build base（與既有單 issue 行為一致）。不引入 dispatch_base 客製化（那是 #175 範疇）。

不選「擴充 `WorktreeCreator.create` 加 repo 參數」：會破壞 `WorktreeCreator` Protocol 與所有 fake/real 實作（`autonomy.py`、測試 `FakeWorktreeCreator`），爆炸半徑大。run-scoped 實例化為 build phase 區域行為，零 Protocol 改動。

### D3 不改動範圍

- `autonomy.py:551` 非 workflow 派工路徑：仍用 dispatcher 共用 creator（Manager repo 為正確 repo）。
- `work_bridge._pr_metadata`：已 iter 全部 `run.issue_refs` 產 `Closes #N`，不改正式邏輯，僅補測試鎖定 multi-issue。
- `dispatcher.py` / `manager_daemon.py:790` 共用 creator 構造：不動（仍供非 workflow 路徑用）。

## 風險

- monitor branch-name correlation 對 multi-issue 仍命中 primary issue 編號（因 branch 含 primary）；其餘 issue 經 closingIssuesReferences 收斂——以測試鎖定。
- 跨 repo build：`run.workspace_root` 必須是可信 repo（`resolve_trusted_repo_root` 已在 claim 時驗證）；build phase 僅取用既有 `run.workspace_root`，不新增信任邊界。
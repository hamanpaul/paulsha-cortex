---
status: accepted
work_item: reclaim-pr-inheritance
---

# reclaim-pr-inheritance Design

## Decisions

### D1 新 run `pr_refs=()`（不繼承 mapped_prs）

`work_bridge._claim_workflow_run` 兩處建立新 run 的 `pr_refs`：
- needs_human 路徑（`registry._manager_create_workflow_run(..., pr_refs=tuple(f"{repo}#{n}" for n in authority.mapped_prs))`）。
- start 路徑（`apply_workflow_action` args `pr_refs=[...]`）。

兩處皆改為 `pr_refs=()`（或空 list）。理由：`pr_refs` 表「本 run 的 delivery PR」；新 run 尚未 delivery，不應有 PR。舊 run 的 PR 由該 run 自己的 `pr_refs` 表達，superseded run 的 monitor 連結已被 `status != "superseded"` 過濾（`providers.py:309`）。

不選「保留繼承但於 delivery 時覆寫」：delivery 路徑已假設 `pr_refs` 為空時開新 PR、非空時用既有 PR（`work_bridge.py:1255-1318`）；繼承舊 PR 會走「既有 PR」分支並卡死（舊 PR 已關閉無法 reopen）。從源頭 `pr_refs=()` 讓新 run 走「開新 PR」分支。

### D2 terminal provider 過濾 closed-unmerged

`GitHubTerminalProvider._scan`（`providers.py:865-880`）遍歷 `pull_nodes` 建 `closing_links`。對 `pull["state"] == "CLOSED"` 且 `pull.get("mergeCommit")` 為空（未 merge）的 PR，跳過 `closing_links` 建立。OPEN 與 MERGED（有 mergeCommit）保留。

判定：`closed_unmerged = state == "CLOSED" and not (merge_commit_oid)`。MERGED PR 的 `state == "MERGED"`（非 CLOSED），不受影響；CLOSED 且有 mergeCommit 不存在於 GitHub 語意（merged PR state=MERGED），故 `state=="CLOSED"` 即未合併。

不選「過濾所有 CLOSED」：未來若有「關閉又 reopen」語意變化，保留 CLOSED 但 merged 的判斷較保守；但 GitHub 已關閉未合併即廢棄，本設計直接以 `state=="CLOSED"` 判定廢棄（merged 永為 MERGED state）。

### D3 不動範圍

- `claim.py` `mapped_prs` 仍如實反映 confirmed github_pr sources（含舊 open PR）；但新 run 不再以它為 `pr_refs`（D1）。`mapped_prs` 仍用於 `_canonical_workflow_run` 的既有 run 比對（既有 delivery run 的 `pr_refs` 比對 `mapped_prs`）——既有 run 的 `pr_refs` 在 delivery 時設為 `(pr,)`，與 `mapped_prs` 一致，不退化。
- delivery journal row 建立時序（`_load_work_run` 已 create-on-missing）不動；R1 消除繼承後新 run 不會誤入既有 PR 的 journal 路徑。
- `rebase-candidate` / `retry-build --rebase-onto` 不實作（enhancement，後續 issue）。

## 風險

- D1 改 `pr_refs` 起始：須確認 monitor 對「ongoing run 無 pr_refs」的處理不退化（ongoing run 在 delivery 前本就可能 `pr_refs=()`，既有測試已涵蓋）。
- D2 改 terminal provider：須確認 `closing_links` 移除 closed-unmerged 後，`mapped_prs` 對「曾關閉又 reopen 的 PR」仍能在 reopen 後重新關聯（reopen → state OPEN → 重新產 link）。以測試鎖定。
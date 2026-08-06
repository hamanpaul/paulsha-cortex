---
status: accepted
work_item: reclaim-pr-inheritance
---

# reclaim-pr-inheritance Design

## Decisions

### D1 新 run `pr_refs=()`（不繼承 mapped_prs）

`work_bridge._claim_workflow_run` 兩處建立新 run 的 `pr_refs` 改為 `()`。`pr_refs` 表「本 run 的 delivery PR」；新 run 尚未 delivery，不應有 PR。舊 run 的 PR 由該 run 自己的 `pr_refs` 表達，superseded run 的 monitor 連結已被 `status != "superseded"` 過濾。

delivery 路徑已假設 `pr_refs` 為空時開新 PR、非空時用既有 PR；繼承舊 PR 會走「既有 PR」分支並卡死（舊 PR 已關閉無法 reopen）。從源頭 `pr_refs=()` 讓新 run 走「開新 PR」分支。

### D2 terminal provider 過濾 closed-unmerged

`GitHubTerminalProvider._scan` 對 `state == "CLOSED"` 的 PR 跳過 `closing_links`；OPEN 與 MERGED 保留。GitHub merged PR 的 state 為 `MERGED`（非 CLOSED），故 `state=="CLOSED"` 即未合併廢棄 PR。

### D3 不動範圍

`mapped_prs` 仍如實反映 confirmed github_pr sources；`_canonical_workflow_run` 對既有 delivery run 的 `pr_refs` 比對 `mapped_prs` 不變。`rebase-candidate` / `retry-build --rebase-onto` 不實作（enhancement，後續 issue）。
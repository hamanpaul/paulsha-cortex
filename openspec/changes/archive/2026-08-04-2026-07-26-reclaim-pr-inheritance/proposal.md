---
status: accepted
work_item: reclaim-pr-inheritance
---

## Goals

讓 re-claim 建立的新 WorkflowRun 不繼承舊 run 的 PR 綁定（`pr_refs=()` 起始），並讓 terminal provider 不再以已關閉未合併 PR 的凍結 `closingIssuesReferences` 貢獻 `mapped_prs`，消除 #175 的 delivery 糾纏根因。

## Why

re-claim 後新 run 以 `authority.mapped_prs` 建立 `pr_refs`，繼承舊 run 的 PR；舊 PR 關閉後 GitHub 凍結其 closing refs，terminal provider 仍頑固關聯，新 run 綁死已關閉 PR 無法開新 PR 交付。

## What Changes

- `coordinator/work_bridge.py`：新 run claim 的 `pr_refs` 改 `()`（needs_human 與 start 兩路徑）。
- `monitor/providers.py` `GitHubTerminalProvider`：`state=="CLOSED"` 的 PR 跳過 `closing_links`。
- `tests/test_reclaim_pr_inheritance.py`：新 run `pr_refs=()`、terminal provider closed-unmerged 過濾、OPEN 回歸。

## Capabilities

### Modified Capabilities
- `coordinator/work-claim`：新 run 不繼承舊 PR。
- `monitor/terminal-provider`：closed-unmerged PR 不貢獻 closing 關聯。
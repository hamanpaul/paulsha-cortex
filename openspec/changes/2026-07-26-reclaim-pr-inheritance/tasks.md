---
status: accepted
work_item: reclaim-pr-inheritance
---

# Tasks

- [ ] 1.1 RED：`tests/test_reclaim_pr_inheritance.py` 涵蓋新 claim run `pr_refs=()`、terminal provider 跳過 closed-unmerged closing_links、OPEN PR 回歸。
- [ ] 1.2 `coordinator/work_bridge.py` `_claim_workflow_run`：新 run `pr_refs=()`（needs_human + start 兩路徑）（#175）。
- [ ] 1.3 `monitor/providers.py` `GitHubTerminalProvider`：`state=="CLOSED"` PR 跳過 `closing_links`（#175）。
- [ ] 1.4 `changelog.d/reclaim-pr-inheritance.md` 與 `CHANGELOG.md [Unreleased] ### Fixed`（#175）；README 同步（R-18）。
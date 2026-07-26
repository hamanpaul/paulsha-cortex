---
status: accepted
work_item: reclaim-pr-inheritance
---

# reclaim-pr-inheritance Specification

#175：re-claim 後新 run 繼承舊 run 的 PR 綁定，使 delivery 進入不可恢復的糾纏（delivery journal 孤兒、無法開新 PR）。

## 背景

當一個 run 已進到 delivery（已開 PR）後需重建（例：dispatch_base 變動需 rebase），operator `cortex work start` 重 claim：
- 舊 run 標 `superseded`（正確），但**新 run 繼承舊 run 的 PR 綁定**（`pr_refs=(舊PR,)`），因 `work_bridge` 以 `authority.mapped_prs` 建立新 run 的 `pr_refs`。
- operator 關閉舊 PR 想讓新 run 開新 PR：GitHub 對已關閉 PR 凍結 `closingIssuesReferences`，terminal provider 重掃後 `mapped_prs` 仍頑固含該已關閉 PR。
- 已關閉且分支已刪的 PR 無法 reopen，新 run 綁死舊 PR、無法交付。

## Requirements

### R1 新 run 不繼承舊 PR 綁定

新 claim 建立的 WorkflowRun MUST 以 `pr_refs=()` 起始，不得繼承 `authority.mapped_prs`。新 run 在 delivery 階段自行建立自己的 PR（既有 `_push_exact_candidate`→`create_or_get_pull_request`→`pr_refs=(new,)` 路徑不變）。

- `work_bridge._claim_workflow_run`：needs_human 建立路徑與 `apply_workflow_action("start")` 的 `pr_refs` 皆改為 `()`（不再取自 `authority.mapped_prs`）。
- 舊 run（superseded）的 PR 連結由 monitor 既有的 `status != "superseded"` 過濾維持，不污染新 run。

### R2 terminal provider 不再關聯已關閉未合併 PR

`GitHubTerminalProvider` 對 `state == CLOSED` 且未 merge 的 PR（closed-unmerged，即廢棄 PR）MUST NOT 產出 `closing_links`。OPEN 與 MERGED PR 的 closing 關聯不變。

- 關閉未合併 PR 的 `closingIssuesReferences` 是 GitHub 凍結的過時證據，不應再貢獻 `mapped_prs`。
- MERGED PR 的 closing 關聯保留（完成證據）。

### R3 限制

- stdlib-only；TDD（先 RED）。
- 不實作 `cortex work rebase-candidate` / `retry-build --rebase-onto`（#175 建議的 in-flight rebase 強化）——屬獨立 enhancement，留待後續 issue；本批只修繼承根因。
- 不改 delivery journal schema；既有單 PR delivery lifecycle 不退化（run 在 delivery 建立自己的 PR 並寫 `pr_refs`）。
- `python3 -m pytest tests/ -q` 全綠；`policy_check --repo .` 0 fail。

## 驗收

- 新增測試：authority.mapped_prs 非空時，新 claim 建立的 run `pr_refs == ()`。
- 新增測試：terminal provider 對 closed-unmerged PR 不產 closing_links；OPEN/MERGED PR 仍產。
- 既有 single-PR delivery lifecycle 測試不退化（run delivery 時建立 PR 並設 `pr_refs`）。
- 既有多 run / superseded 過濾測試不退化。
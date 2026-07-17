# Unified Work Lifecycle 操作與遷移

## 四態 read model

Monitor 對每個 repo/work item 只公開 `topic`、`todo`、`on-going`、`done`。`blocked`、`needs_human`、`degraded` 是 facet，不是第五種狀態。

- `topic`：只有 open GitHub issue，尚無 confirmed Todo artifact。
- `todo`：有 `todo.md`、accepted superpowers spec/plan 或 active OpenSpec，尚未 claim。
- `on-going`：Manager 已建立 `WorkflowRun`；queued 到 ship 都維持此狀態。
- `done`：merge commit、所有 issue closed、default branch active OpenSpec 消失、archive 存在、Todo 完成與 CompletionRecord 全部驗證成功。

Provider 失敗時會保留 last-good snapshot 並標 `degraded`。GitHub provider 超過 900 秒沒有成功 snapshot 時，auto claim 與 merge 都會 fail-closed。

## Correlation authority

可授權 mutation 的關聯只來自：

1. repo 內 `.cortex/work-items.yaml` version 1；
2. Markdown scalar frontmatter `work_item`；
3. GitHub closing reference；
4. Manager workflow metadata。

Title、slug、branch 或 issue token 只形成 inferred display group，不能 start、merge 或判定 done。`cortex list --explain` 會列出 accepted/rejected signals。

Override 範例：

```yaml
version: 1
work_items:
  unified-work-lifecycle:
    title: 統一工作生命週期
    links:
      - kind: github_issue
        ref: owner/repo#14
      - kind: openspec
        ref: unified-work-lifecycle
    excludes:
      - kind: github_pr
        ref: owner/repo#999
```

`unlink` 會留下 exclusion，避免 inferred grouping 下次重新合併。單一 source 若被兩個 confirmed work item claim，整個 provider 會 degraded，Manager 不得派工。

## CLI

```bash
cortex list --repo owner/repo --state todo --explain
cortex work show unified-work-lifecycle --repo owner/repo --json
cortex work link unified-work-lifecycle --repo owner/repo --kind github_issue --ref owner/repo#14
cortex work unlink unified-work-lifecycle --repo owner/repo --kind github_issue --ref owner/repo#14
cortex work start unified-work-lifecycle --repo owner/repo
cortex work resume unified-work-lifecycle --repo owner/repo
cortex work auto unified-work-lifecycle --repo owner/repo --enable
cortex work auto unified-work-lifecycle --repo owner/repo --disable
cortex doctor --probe-live --repo owner/repo --json
```

工作預設 manual。Auto claim 同時要求 confirmed Todo、confirmed issue 與 `cortex:auto-on-going` label；移除 label 只阻止尚未 claim 的工作，不會中止 active workflow。Todo 缺 issue 時不會自動建立 issue，而是 `needs_human: missing_issue`。

## Snapshot 與 registry migration

- Work snapshot：`$PSC_MONITOR_STATE_ROOT/work-items.snapshot.json`；未設定時為 `$PSC_AGENTS_ROOT/monitor/work-items.snapshot.json`。
- Snapshot schema：`work-items-snapshot/v1`，mode `0600`，atomic replace + file/directory fsync。
- Coordinator registry：首次載入合法 v1 時先建立 read-only、content-hash 命名的 backup，再升級為 v2。
- 舊 jobs/slices 只進 `legacy_records`，不會猜測 work item association。
- Unknown/malformed schema 不會覆寫現有合法檔案；先修復或從已驗證 backup 恢復，再 restart service。

## Delivery gate

Manager 是唯一 writer。每次 push 都會使上一個 Copilot epoch 失效，並重新要求 current-HEAD review。Merge 前必須同時具備：

- exact tree 的 policy + pinned preflight；
- deterministic verification 與不同 independence domain 的 ForeignReview；
- current-HEAD、非 error 的 Copilot review，且 threads resolved/outdated；
- terminal-green checks/statuses、closing refs、archive diff 與 mergeability；
- fresh GitHub provider snapshot。

最多兩輪 builder fix/re-review，每個 HEAD 等待 15 分鐘；第三次仍有 finding 或逾時即 `needs_human`。合併只使用 `gh pr merge --merge --match-head-commit <HEAD>`，不使用 auto/squash/rebase。Merge 後會重新 fetch default branch，驗證雙親 merge commit ancestry、issue、archive、Todo 與 CompletionRecord，全部成立才投影 `done`。

V1 terminal delivery 僅支援 GitHub。其他 forge 仍可顯示 read model，但 ship 會停在 `needs_human`。

# Cortex Context and Ubiquitous Language

本文件是 `paulsha-cortex` 的跨模組語彙與責任邊界。功能需求的規範性細節位於 OpenSpec；實作順序位於 superpowers plan。若文件用語衝突，以 `openspec/specs/**` 與目前 active change 的 delta spec 為準。

## 系統定位

`paulsha-cortex` 是治理平面：Monitor 將多來源事實投影為工作 read model，Manager 是 workflow 與 delivery 狀態的單一 writer／orchestrator，Persona 契約限制每一步可做的事。它對 `paulsha-hippo` 維持零 runtime 依賴；只有既有 persona loader 可 lazy import upstream deck schema。

本次 umbrella work item 為 GitHub issue `hamanpaul/paulsha-cortex#14`，stable ID 為 `unified-work-lifecycle`。關聯 issue `#4`、`#5`、`#8`、`#10`、`#12` 仍保有各自 scope；`#14` 不取代或虛假關閉它們。

## 生命週期語彙

| Term | 定義 | 不代表 |
| --- | --- | --- |
| Work Item | 將 issue、Todo artifact、OpenSpec、workflow、PR 與 completion evidence 關聯後形成的使用者可見工作單位 | 單一檔案或單一 Job |
| `topic` | 有 GitHub open issue，但沒有 confirmed authoritative Todo artifact | 可自動派工 |
| `todo` | 有 confirmed Todo artifact，且 Manager 尚未建立 active `WorkflowRun` | 已開始執行 |
| `on-going` | Manager 已建立 active `WorkflowRun`；內部 enum 為 `ongoing` | 一定沒有阻塞 |
| `done` | PR、issue、remote OpenSpec archive、Todo tasks 與 `CompletionRecord` 的 strict closure 全部成立 | agent exit 0、local archive 或 PR close |
| Facet | 生命週期上的正交註記，例如 `blocked`、`needs_human`、`degraded` | 第五種 lifecycle state |
| Phase | workflow 內部進度：`claim`、`define`、`plan`、`build`、`verify`、`review`、`ship` | Work Item lifecycle state |
| Strict closure | 可重讀的遠端與 deterministic evidence 全部通過後，才能投影 `done` 的合取 gate | 多數條件通過或推測完成 |
| Reopen | 已完成工作因 issue reopen 或同名 OpenSpec 重新 active 而退回 `topic`／`todo` | 另建一個不可關聯的新工作 |

Lifecycle reducer 固定優先序：受影響 provider degraded 時保留前次 state；否則 active workflow → `ongoing`，strict closure → `done`，active authoritative artifact → `todo`，其餘 open issue → `topic`。預設 list 隱藏 `done`，`--all` 才顯示。

## 來源與關聯語彙

| Term | 定義 |
| --- | --- |
| Work Source | provider 觀測到的具 revision 來源，kind 為 `github_issue`、`github_pr`、`todo`、`superpowers_spec`、`superpowers_plan`、`openspec`、`workflow_run` 或 `completion_record` |
| Authoritative Todo artifact | repo 內 `todo.md`、superpowers spec/plan 或 active OpenSpec；必須通過解析且未被 archive/exclude |
| Confirmed link | repo override、結構化 `work_item` frontmatter、GitHub closing reference 或 Manager workflow metadata 建立的授權級關聯 |
| Inferred group | 由至少兩個獨立模糊訊號且無競爭候選形成的顯示分組；不能授權 claim、merge 或 done |
| Source revision | provider 可重讀的版本，例如 git blob/tree SHA、GitHub node/updated-at/HEAD tuple 或 workflow immutable ID |
| Exclusion | operator `unlink` 後的 durable negative link，防止模糊訊號把來源再次併回 |
| Provider degraded | provider 本輪無法建立 authoritative snapshot；沿用 last-good sources 並凍結可能造成 removal、done 或 dispatch 的變更 |

Confirmed authority 由高到低固定為：repo override → `work_item` frontmatter → GitHub closing reference → Manager workflow metadata。衝突不以優先序偷偷覆蓋：同一 source 若被兩個 confirmed work item claim，該 repo/provider 進入 `degraded`，Manager fail-closed，等待 operator 修正。

## 固定 repo contract

### Override

每個受管 repo 的唯一 override 路徑為 `.cortex/work-items.yaml`；v1 不搜尋其他檔名。格式如下：

```yaml
version: 1
work_items:
  unified-work-lifecycle:
    title: 統一工作生命週期與 Persona Workflow 強化
    links:
      - kind: github_issue
        ref: hamanpaul/paulsha-cortex#14
      - kind: openspec
        ref: unified-work-lifecycle
      - kind: path
        ref: docs/superpowers/specs/2026-07-17-unified-work-lifecycle.md
    excludes:
      - kind: github_pr
        ref: hamanpaul/paulsha-cortex#999
```

`version` 必須恰為 `1`；work ID 必須符合 `[a-z0-9][a-z0-9-]*`；未知 key、重複 source、同一 source 同時 link/exclude、跨 work item confirmed collision 全部 fail-closed。`path` 必須是 repo-relative、不得含 `..` 或 symlink escape。`cortex work link/unlink` 以 temp file、fsync、`os.replace` 原子更新此檔；`unlink` 必須新增 exclusion。

### Frontmatter

Todo、superpowers spec/plan 與 active OpenSpec markdown 可使用唯一關聯 key：

```yaml
---
work_item: unified-work-lifecycle
---
```

`work_item` 必須是單一非空 slug，不接受 list、issue number shorthand 或 title。缺 key 可進 inferred display group，但不能授權 claim。不同 artifact 的 explicit `work_item` 若使同一 source 產生 confirmed collision，provider degraded。

### Durable last-good snapshot

Monitor state root 由 `PSC_MONITOR_STATE_ROOT` 覆寫；預設為 `$PSC_AGENTS_ROOT/monitor`。唯一 v1 snapshot 路徑為：

```text
$PSC_MONITOR_STATE_ROOT/work-items.snapshot.json
```

檔案 mode 為 `0600`，schema 為 `work-items-snapshot/v1`：

```json
{
  "schema": "work-items-snapshot/v1",
  "sequence": 42,
  "written_at": "2026-07-17T10:00:00Z",
  "providers": {
    "github:hamanpaul/paulsha-cortex": {
      "status": "ok",
      "last_attempt_at": "2026-07-17T10:00:00Z",
      "last_success_at": "2026-07-17T10:00:00Z",
      "revision": "github-snapshot:opaque-revision",
      "diagnostics": [],
      "sources": []
    },
    "repo:hamanpaul/paulsha-cortex": {
      "status": "degraded",
      "last_attempt_at": "2026-07-17T10:00:00Z",
      "last_success_at": "2026-07-17T09:55:00Z",
      "revision": "git-tree:opaque-revision",
      "diagnostics": ["scan unavailable"],
      "sources": []
    }
  },
  "work_items": [],
  "source_owners": {},
  "exclusions": []
}
```

`sources` 與 `work_items` 使用下方 JSON API 的 canonical object。成功 provider scan 才能替換該 provider 的 sources/revision/last-success；失敗只更新 attempt/status/diagnostics，保留 last-good sources。整檔以 temp + file fsync + atomic replace + directory fsync 寫入；unknown schema、parse error 或 ownership collision 不覆寫既有 last-good snapshot。啟動時可讀合法 snapshot 立即提供 degraded read model，再背景 refresh。超過 900 秒沒有 GitHub success 時禁止 auto claim 與 merge。

## Workflow 與 Persona 語彙

| Term | 定義 |
| --- | --- |
| WorkflowRun | Manager 建立且持久化的工作 attempt aggregate；建立後 Work Item 即為 `ongoing` |
| WorkflowStep | 一個 phase 的 persona/card/executor/model/domain、inputs、outputs 與 gate result |
| Claim key | `repo + work_id + authoritative source revisions` 的 stable digest；restart 不得重複建 job、branch 或 PR |
| Combo | workflow step graph；v1 default 為 `feature-oneshot` |
| Persona binding | Deck card 指定的 planner/builder/reviewer/manager，必須編入 workflow manifest，不能用 global builder 覆蓋 |
| Independence domain | model identity registry 對 executor/model 的供應商級獨立性分類：`google`、`anthropic`、`openai` 等 |
| Brainstorm peer | completeness gate 的異質 secondary planner，只回 evidence，不做 final integration |
| Foreign reviewer | build 後在 detached exact HEAD 執行語意審查的異質 reviewer；與 brainstorm peer 是兩個獨立 gate |

Manager 是 WorkflowRun、WorkflowStep、claim、PR mutation 與 CompletionRecord 的唯一 writer。Monitor 與 CLI read command 不得直接改 workflow registry。所有 mutation 經 control request queue；冪等 key 重送只回既有結果。

## Delivery 語彙

| Term | 定義 |
| --- | --- |
| Current-HEAD review | Copilot review `commit_id` 等於 PR 目前 HEAD，且 review 非 error |
| Terminal-green | 所有 required checks 已完成且 conclusion 為 success/neutral/skipped 中政策允許者；pending、failed、cancelled、timed-out 都不是 green |
| Exact-tree evidence | full suite evidence 綁定目前 tree hash；只有近期且完全一致才可 `--skip-tests` |
| Remote archive | change 已從 authenticated GitHub default branch 的 `openspec/changes/<name>` 消失，且 `openspec/changes/archive/**/<name>` 存在 |
| CompletionRecord | strict closure 的 immutable、versioned、hash-bound 最終證據；只是必要條件之一，不單獨等於 `done` |

Merge 固定使用 merge commit；不使用 GitHub auto-merge。Copilot finding 最多兩輪 builder fix/re-review，每個 HEAD 最長等待 15 分鐘；逾時或第三輪仍有 finding 時設 `needs_human`，不得替換 reviewer 或繞過 merge gate。

## CLI JSON contract

所有 `--json` 成功輸出單一 JSON object，不混入 human text；錯誤寫 stderr 並使用非零 exit code。共同 envelope：

```json
{
  "schema": "cortex-work/v1",
  "generated_at": "2026-07-17T10:00:00Z",
  "sequence": 42,
  "degraded": false,
  "providers": [],
  "items": []
}
```

`cortex list --json` 使用 `items`（可為空）；`cortex work show <id> --json` 使用 `item`，沒有 `items`；`cortex work show --explain --json` 另加 `explanation`。canonical WorkItem object：

```json
{
  "work_id": "unified-work-lifecycle",
  "repo": "hamanpaul/paulsha-cortex",
  "title": "統一工作生命週期與 Persona Workflow 強化",
  "state": "on-going",
  "phase": "plan",
  "facets": ["degraded"],
  "sources": [
    {
      "source_id": "github_issue:hamanpaul/paulsha-cortex#14",
      "kind": "github_issue",
      "ref": "hamanpaul/paulsha-cortex#14",
      "revision": "opaque-provider-revision",
      "status": "open",
      "confidence": "confirmed",
      "provider": "github:hamanpaul/paulsha-cortex"
    }
  ],
  "next_actions": ["start"],
  "workflow_run_id": null,
  "updated_at": "2026-07-17T10:00:00Z"
}
```

陣列固定依 `repo, work_id` 或文件指定 key 排序；facets/next-actions 去重後 lexical sort。`state` 永遠輸出 `on-going`，即使內部 enum 為 `ongoing`。未知欄位可由 minor release additive 新增；刪除、改名或語意變更需新 schema URI。

`explanation` 固定為 `{work_id, authoritative_links, inferred_signals, competing_candidates, exclusions, reducer_trace}`；每個 signal 含 `kind`、`value`、`source_ids`、`weight`、`accepted`、`reason`。它只解釋，不改變 authority。

## Safety invariants

1. 沒有 confirmed Todo + confirmed issue 映射，auto label 也不能 claim。
2. Provider degraded 不得造成 source removal、`done`、新 dispatch 或 merge。
3. 模糊訊號不得授權 claim、PR mutation、merge 或 completion。
4. Planner peer、Builder、Foreign Reviewer 的 independence domain 必須依 gate 分離；未知 identity fail-closed。
5. Agent exit、local archive、PR closed、issue closed 任一單獨事件都不能投影 `done`。
6. CLI mutation 不直接寫 registry；Manager 是唯一 writer。
7. `cortex:auto-on-going` 移除只影響尚未 claim 的 work item，不中止 active workflow。

## Canonical artifacts

- Architecture decision：`docs/adr/0001-unified-work-lifecycle-authority.md`
- Accepted design spec：`docs/superpowers/specs/2026-07-17-unified-work-lifecycle.md`
- Implementation plan：`docs/superpowers/plans/2026-07-17-unified-work-lifecycle.md`
- OpenSpec change：`openspec/changes/unified-work-lifecycle/`

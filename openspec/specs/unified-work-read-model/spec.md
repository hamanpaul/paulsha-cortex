# unified-work-read-model Specification

## Purpose
TBD - created by archiving change unified-work-lifecycle. Update Purpose after archive.
## Requirements
### Requirement: Monitor必須聚合versioned Work Sources
Monitor MUST從authenticated GitHub、repo artifacts與Manager registry讀取versioned `WorkSource`，並投影`WorkItem`。Repo provider MUST掃描`docs/superpowers/workstreams/**/todo.md`、`docs/superpowers/specs/**/*.md`、`docs/superpowers/plans/**/*.md`與`openspec/changes/*`的proposal/design/tasks/spec，且 MUST明確排除`openspec/changes/archive/**`。Local uncommitted artifact MAY使item成為`todo`，但 MUST NOT作為`done` evidence。

#### Scenario: 四種repo artifact投影為todo
- **WHEN** repo provider成功掃到todo、superpowers spec、superpowers plan或active OpenSpec任一合法artifact
- **THEN** Monitor建立或更新對應WorkSource
- **THEN** 尚無active WorkflowRun的confirmed work item投影為`todo`

#### Scenario: Archive不算active Todo
- **WHEN** change只存在於`openspec/changes/archive/**`且沒有其他active Todo artifact
- **THEN** repo provider不產生active openspec source
- **THEN** 系統不得只憑archive投影`todo`

#### Scenario: Active與archive同名
- **WHEN** 同一change同時存在active與archive copy
- **THEN** repo provider標`degraded`並列出collision diagnostic
- **THEN** Manager不得claim、merge或投影done

### Requirement: Provider失敗必須保留durable last-good
Monitor MUST將provider snapshots與WorkItems原子保存於`$PSC_MONITOR_STATE_ROOT/work-items.snapshot.json`；未設定時root MUST為`$PSC_AGENTS_ROOT/monitor`，schema MUST為`work-items-snapshot/v1`且file mode MUST為`0600`。Provider只有成功完整scan後 MAY替換其sources/revision/last-success；auth、rate-limit、timeout、I/O、parse或ownership collision MUST保留last-good sources並標`degraded`。Unknown/invalid snapshot MUST NOT覆寫既有合法snapshot。

GitHub terminal closure provider讀取remote Todo時 MUST以authenticated default revision呼叫Contents API，且 MUST逐筆驗證response為file、path與tree entry相同、blob SHA與tree revision相同、encoding為base64。Production ancestry compare MUST只對canonical WorkflowRegistry `workflow_links`已連結至該repo的PR執行；其他PR仍可提供remote state但 MUST NOT取得`merged_with_merge_commit=true`的closure authority。只有明確HTTP 502/503/504 MAY以finite bounded delay重試；auth、rate-limit、其他HTTP error、malformed JSON或identity mismatch MUST立即fail-closed並保留last-good。

#### Scenario: GitHub暫時timeout
- **WHEN** GitHub provider上次成功後本輪timeout
- **THEN** snapshot保留上次GitHub sources與revision並更新degraded health
- **THEN** 受影響work item不得因本輪failure被remove、done、claim或merge

#### Scenario: Restart載入last-good
- **WHEN** Monitor restart且durable snapshot合法但providers尚未完成live refresh
- **THEN** Monitor先提供snapshot內的WorkItems並加degraded facet
- **THEN** background成功refresh後才替換provider sources

#### Scenario: GitHub freshness超過900秒
- **WHEN** 距最後成功GitHub snapshot超過900秒
- **THEN** read API仍可顯示last-good state與diagnostic
- **THEN** Manager拒絕auto claim與merge

#### Scenario: 無關merged PR不觸發terminal ancestry查詢
- **WHEN** repo含多張歷史merged PR但WorkflowRegistry只把其中一張連結到active work item
- **THEN** terminal provider只對該workflow-linked PR查詢merge revision是否已進default branch
- **THEN** 無關PR不得取得closure authority，也不得因其compare API暫時失敗拖垮本輪scan

### Requirement: Correlation必須區分confirmed與inferred authority
Confirmed association MUST只來自repo root `.cortex/work-items.yaml` version 1、markdown scalar frontmatter key `work_item`、GitHub closing reference或Manager workflow metadata。Override work ID MUST符合`[a-z0-9][a-z0-9-]*`；path MUST為repo-relative且不可escape。Title、slug、branch與issue token等heuristic MUST至少有兩個獨立訊號、無competing candidate，且只能建立`inferred` display group。單一source MUST NOT屬於兩個confirmed work item；collision MUST令provider degraded。`unlink` MUST保存exclusion。

#### Scenario: Label存在但只有inferred association
- **WHEN** Todo與auto-labeled issue只有title/slug heuristic關聯
- **THEN** Monitor可在explain中顯示inferred group
- **THEN** Manager不得因此claim或merge

#### Scenario: Override修正後restart
- **WHEN** operator以`work link`寫入confirmed link並restart Monitor
- **THEN** `.cortex/work-items.yaml`仍建立相同stable work ID與ownership
- **THEN** association不依賴舊in-memory heuristic

#### Scenario: Unlink排除模糊重合
- **WHEN** operator unlink一個source
- **THEN**原confirmed link移除且exclusion原子寫入override
- **THEN**後續heuristic不得把source重新合回該work item

### Requirement: Lifecycle reducer必須依嚴格優先序且可回退
公開state MUST只有`topic|todo|on-going|done`，內部第三態 MUST為`ongoing`；`blocked|needs_human|degraded` MUST為facets。Provider degraded時 MUST freeze prior state；其餘依active WorkflowRun→`ongoing`、strict closure→`done`、active confirmed artifact→`todo`、其餘open issue→`topic`。Strict closure MUST同時要求mapped PR以merge commit合併、所有mapped issues closed、remote default branch active OpenSpec消失且archive存在、Todo tasks完成、CompletionRecord有效。

#### Scenario: Issue只有auto label
- **WHEN** open issue帶`cortex:auto-on-going`但沒有confirmed Todo artifact與WorkflowRun
- **THEN** state維持`topic`
- **THEN** Manager不得auto claim

#### Scenario: 部分closure不能done
- **WHEN** merge、issue close、archive、Todo completion或CompletionRecord任一缺失
- **THEN** item不得投影`done`

#### Scenario: 已完成issue reopen
- **WHEN** done item的mapped issue reopen
- **THEN** reducer撤銷done並依active artifact投影`todo`或`topic`
- **THEN**舊CompletionRecord只保留audit

### Requirement: CLI與socket必須提供versioned可解釋read API
系統 MUST提供`cortex list [--repo --state --all --json --explain]`與`cortex work show <work-id>`。JSON MUST使用`cortex-work/v1` envelope與`CONTEXT.md`定義的canonical WorkItem/WorkSource；輸出state MUST拼為`on-going`。Monitor socket MUST新增`list_work_items`、`get_work_item`、`explain_work_item`與work-item subscription。既有ProjectState API MUST相容一個release cycle。

#### Scenario: Default list隱藏done
- **WHEN** operator呼叫`cortex list`而未指定`--all`
- **THEN**只列topic、todo與on-going

#### Scenario: Explain顯示競爭訊號
- **WHEN** operator以`--explain --json`查詢inferred或conflicted item
- **THEN** response包含authoritative links、inferred signals、competing candidates、exclusions與reducer trace
- **THEN** explain本身不修改association


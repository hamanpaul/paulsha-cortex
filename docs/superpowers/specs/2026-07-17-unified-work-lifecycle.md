---
work_item: unified-work-lifecycle
status: accepted
issue: hamanpaul/paulsha-cortex#14
---

# Unified Work Lifecycle and Persona Workflow — Accepted Design

## 1. Problem and outcome

Monitor要從ProjectState scanner升級為跨repo工作read model；Manager要從Slice completion manager升級為Work Item workflow與delivery的唯一writer。使用者只看到四個lifecycle state：

```text
topic -> todo -> on-going -> done
```

`blocked`、`needs_human`、`degraded`是facets；`claim/define/plan/build/verify/review/ship`是workflow phases。任何provider failure、模糊關聯、舊HEAD review或部分closure都必須fail-closed。

Umbrella issue是`hamanpaul/paulsha-cortex#14`。Issue #4/#5/#8/#10/#12保有獨立scope；本work item只以各PR實際完成內容更新它們。

## 2. Fixed public contracts

### 2.1 Source coverage

- Authenticated `gh api`: issues、PR、closing refs、reviews、threads、checks、default branch tree/archive。
- Repo: `docs/superpowers/workstreams/**/todo.md`、`docs/superpowers/specs/**/*.md`、`docs/superpowers/plans/**/*.md`、active `openspec/changes/*/{proposal,design,tasks,specs/**}`。
- `openspec/changes/archive/**`永遠不是active Todo source。
- Manager registry/evidence: WorkflowRun、WorkflowStep、CompletionRecord。
- Local uncommitted artifacts只作todo overlay，不能作done evidence。

### 2.2 Correlation authority

Confirmed link只接受以下來源，且source ownership必須唯一：

1. Repo `.cortex/work-items.yaml` version 1。
2. Markdown scalar frontmatter `work_item: <slug>`。
3. GitHub closing reference。
4. Manager workflow metadata。

Override schema、negative exclusion與path safety以`CONTEXT.md`為canonical。Title/slug/branch/issue token需要至少兩個獨立訊號且無競爭候選，仍只可標`inferred`，不得授權claim、merge或done。`--explain`必須列出所有accepted/rejected signals。

### 2.3 Durable read model

- Root env: `PSC_MONITOR_STATE_ROOT`；default `$PSC_AGENTS_ROOT/monitor`。
- File: `$PSC_MONITOR_STATE_ROOT/work-items.snapshot.json`。
- Schema: `work-items-snapshot/v1`，mode `0600`，temp+fsync+replace+directory fsync。
- Provider成功才替換sources；failure保留last-good並更新degraded health。
- Startup可先讀snapshot提供degraded state；invalid/unknown schema不覆寫合法檔。
- GitHub default refresh 300秒；900秒沒有success時禁止auto claim與merge。

### 2.4 CLI/API

- `cortex list [--repo ...] [--state ...] [--all] [--json] [--explain]`，default隱藏done。
- `cortex work show|link|unlink|start|resume <work-id>`。
- `cortex work auto <work-id> --enable|--disable`管理`cortex:auto-on-going`；未指定 legacy issue selector 時，對全部 confirmed mapped issues 套用同一 mutation，任一 API 失敗整體 fail-closed。
- JSON schema為`cortex-work/v1`；canonical envelope/object/explanation在`CONTEXT.md`。
- Input filter接受`ongoing|on-going`，human/JSON output只顯示`on-going`。
- Socket新增list/get/explain/subscription；ProjectState API相容一個release cycle。

## 3. Data model

`WorkItem`至少保存`work_id, repo, title, state, phase, facets, sources, next_actions, workflow_run_id, updated_at`。`WorkSource`至少保存`source_id, kind, ref, revision, status, confidence, provider`。

`WorkflowRun`保存work item、claim key、combo、current phase、steps、issue/OpenSpec/PR refs、attempts、evidence。`WorkflowStep`保存phase、persona、card、executor/model/domain、inputs、outputs與gate result。

每個非Manager card由production dispatcher建立durable Job，綁定run/claim/repo/source revision/phase/card/persona/model identity。Terminal poll只從job log建立canonical coordinator-root evidence並原子綁回job；control caller不得提供evidence path/hash。同phase全部card passed後才前進，restart後從registry resume。

Claim key 只綁該 work item 的 canonical semantic authority 與 provider/source revisions，不綁 snapshot sequence、written-at 或其他 repo 資料。V1 terminal delivery 每個 run 只支援唯一 PR、OpenSpec 與 Todo target；任一類有多個 confirmed refs 時必須 `needs_human`，不得以單一 target 產生 CompletionRecord。Repo mutation 的 `repo_root` 必須等於 canonical git top-level realpath 且 origin identity 相符。

Stable ID優先explicit work item，其次`issue:<owner>/<repo>#N`，最後source locator。內部state enum用`ongoing`。

## 4. State reducer

1. 受影響provider degraded：freeze prior state，加`degraded` facet。
2. 有active WorkflowRun：`ongoing`。
3. Strict closure全部成立：`done`。
4. 有confirmed active Todo artifact：`todo`。
5. 其餘open issue：`topic`。

Strict closure是合取：mapped PR以merge commit進default branch、all mapped issues closed、remote active OpenSpec消失、remote archive存在、Todo tasks complete、CompletionRecord revisions/hashes有效。Issue reopen或OpenSpec re-activate必須退回topic/todo。

## 5. Manager workflow

### 5.1 Claim

Default manual。Manual start需要confirmed Todo。Auto claim同時需要confirmed Todo、confirmed GitHub issue與auto label。Issue-only不dispatch；Todo缺issue成`needs_human:missing_issue`，不得自動建issue。Label removal不取消active run。Claim key綁repo/work ID/source revisions，restart必須冪等。

Registry以atomic backup從v1升v2；v1 jobs/slices保留legacy records，不猜work association。CLI mutation只送control request，Manager是唯一writer。

### 5.2 Define/plan

新增planner persona；Deck compiler保存每card `persona_binding`，default combo `feature-oneshot`。Artifact只有在frontmatter `status: accepted`、必要章節存在且沒有blocking decision marker時才算accepted。Blocking marker只接受獨立行`TBD`、`[TBD]`、`Decision: TBD`、`決策：未定`或Open Questions章節中的實際項目；inline說明文字與fenced code不觸發。缺accepted spec/design/plan或存在blocking marker時，primary planner先出question pack，secondary異質planner只回evidence，primary整合落檔。

所有Define/Plan invocation與manifest plan card只在temporary disposable checkout以plan/read-only/sandbox執行；Claude不得使用`acceptEdits`且停用tools，Codex固定`--sandbox read-only`。成功、nonzero與exception都驗sandbox/operator tree；snapshot權限錯誤也先恢復安全traversal，再依baseline還原entries、mode與xattrs，restore fault fail-closed。Scan時持久化canonical ref/kind/work item/content hash authority；Primary structured replacement必須逐欄符合該authority與manifest refs，不接受caller hash或filename推測。新檔no-clobber。Artifacts、immutable/idempotent brainstorm evidence、expected gate ref與registry phase update共用durable intent journal；registry未commit才rollback，已commit則restart逐operation驗type/hash/mode/evidence，drift成`needs_human`並保留journal。

Secondary selection: `agy/google -> claude/anthropic -> codex/openai`，排除primary domain。`agy`只使用headless print + plan + sandbox，不允許unsafe bypass；`agy + Gemini 3.1 Pro (High)`映射google並由live doctor驗證。沒有異質model或output malformed時停needs_human。

### 5.3 Build/verify/review

每張card dispatch時保存output目錄baseline。Verify/review card除canonical coordinator evidence外，必須實際產生符合manifest `produces` glob、為該job新建或相對baseline更新的report；report frontmatter精確綁run/card/Candidate，canonical evidence保存current/baseline hash且path只屬gate ref，不得拿來滿足report output。

Builder在`feature/<issue>-<slug>` worktree；沿用exact Candidate、base comparison、artifact/evidence verification。Foreign Reviewer使用不同domain、detached exact HEAD。Brainstorm peer、ForeignReview、Copilot是三個不同evidence gate，不可互換。

### 5.4 Ship

Manager依序：

1. `openspec archive -y <change>`，驗tasks/canonical specs/doc refs/changelog。
2. 擬定zh-TW conventional PR title/body/labels，body用`Closes #N`。
3. 跑快速policy，再跑`PSC_PREFLIGHT_CMD`；preflight含pytest/OpenSpec/PR-context policy。
4. 開/更新PR，等待checks terminal-green，每次push重請current-HEAD Copilot。
5. 驗Copilot非error、`commit_id == HEAD`、threads resolved/outdated。
6. Finding最多兩輪builder fix/re-review，每HEAD 15分鐘；逾限needs_human。
7. Merge前重讀HEAD/mergeability/checks/threads/issues/archive；只用`gh pr merge --merge`。
8. Merge後fetch default，驗ancestry/issues/archive/Todo，再寫CompletionRecord。

只有exact current tree的fresh full-suite evidence可`--skip-tests`。不使用GitHub auto-merge。

## 6. Rollout

- Baseline: issue #4 scan stability、stale active OpenSpec cleanup、issue #10 evidence check。
- PR A: provider/snapshot/reducer/correlation/read CLI/socket。
- PR B: planner/manifest/registry v2/agy/brainstorm。
- PR C: claim/actions/preflight/Copilot/merge/remote closure。
- PR D: doctor/help/docs/service/migration/archive/canary。

Canary只在`paulsha-cortex`建立低風險docs-only issue，刻意缺accepted plan，證明heterogeneous brainstorm到done全鏈。通過前其他repo不得啟用auto label；Monitor read-only可先部署。

## 7. Acceptance matrix

| Area | Required evidence |
| --- | --- |
| Provider | auth/rate-limit/timeout、scan race、invalid interval、restart cache、success-only removal |
| Lifecycle | four artifact todo、archive exclusion、active/archive collision、partial closure、reopen |
| Correlation | fuzzy no authority、include/exclude、same-name collision、restart persistence、explain |
| Workflow | manual default、label-only auto、issue-only no dispatch、missing issue、claim idempotency |
| Persona | step bindings、planner/builder/reviewer domains、agy unavailable/same-domain/malformed fail-closed |
| GitHub | old-HEAD/error review、unresolved thread、new push、failed/cancelled checks、HEAD race |
| Compatibility | ProjectState APIs、slice actions、CompletionRecord、registry v1 fixtures |
| Deployment | live doctor與single-repo canary完整closure |

每個code PR必須更新changelog，通過full pytest、`openspec validate --all --strict`、policy、pinned preflight與independent review。多worktree整合後再跑full integration suite。

## 8. Non-goals and safety limits

- V1 terminal delivery只支援GitHub；其他forge顯示read model但ship為needs_human。
- 不自動建立missing issue，不用heuristic授權mutation。
- 不導入journal/database、batch integration或自動merge queue。
- Canary前不fleet-enable auto label。
- Provider degraded、identity unknown、evidence stale或任何gate exception一律fail-closed。

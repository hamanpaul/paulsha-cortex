# Unified Work Lifecycle 操作與遷移

對應 OpenSpec 已於 2026-07-20 由 official CLI 封存至 `openspec/changes/archive/2026-07-20-unified-work-lifecycle/`；governed delivery closure、persona workflow orchestration 與 unified work read model 已發佈至 `openspec/specs/` 作為 canonical 規格。

## 四態 read model

Monitor 對每個 repo/work item 只公開 `topic`、`todo`、`on-going`、`done`。`blocked`、`needs_human`、`degraded` 是 facet，不是第五種狀態。

- `topic`：只有 open GitHub issue，尚無 confirmed Todo artifact。
- `todo`：有 `todo.md`、accepted superpowers spec/plan 或 active OpenSpec，尚未 claim。
- `on-going`：Manager 已建立 `WorkflowRun`；queued 到 ship 都維持此狀態。
- `done`：merge commit、所有 issue closed、有效 CompletionRecord 與可驗證的遠端 Todo evidence 全部成立；workstream Todo 未勾 checkbox 只作診斷、不阻擋 `done`。若 work item 有 mapped OpenSpec，仍要求 default branch active OpenSpec 消失且 archive 存在，archived tasks 完成檢查維持不變。

### 管線外交付的結案（#895）

若交付發生在 Cortex 管線外，work item 沒有任何 `WorkflowRun`，operator 可執行 `cortex work close-delivered <work-id> --repo <owner/repo> --actor <actor> --reason <reason>`。Manager 會即時重驗所有 mapped issue 已關閉、唯一 mapped PR 已以 merge commit 進入 default branch、mapped OpenSpec 已封存且不再 active，以及 mapped Todo 與 archived OpenSpec tasks 全部完成；其中任一 remote fact 不符或無法驗證都不會寫入紀錄。V1 要求至少一個 issue、唯一一張 PR、至少一個 mapped Todo，並至多一個 mapped OpenSpec change。

驗證成功後，Manager 會寫入含 actor、reason、authority 與遠端 closure facts 的 immutable `cortex-work-close-delivered/v1` CompletionRecord。Monitor 只把這類固定目錄中 hash 與唯讀檔案驗證通過的紀錄納入 CompletionRecord 投影；原有 `done` reducer 與所有 closure 條件維持不變，不會建立或偽造 `WorkflowRun`。若其他 strict closure evidence 尚未成立，work item 仍不會投影為 `done`。

Provider 失敗時會保留 last-good snapshot 並標 `degraded`。GitHub provider 超過 900 秒沒有成功 snapshot 時，auto claim 與 merge 都會 fail-closed。

### Workflow admission 的 executor backoff

Workflow lane 在 runtime preflight 與 provider-failure reroute 之前，會先讀 durable
executor-backoff store 的 executor×model cooldown。active cooldown 只會在既有候選順序上
被跳過，不會把 run 打進 `needs_human`；若仍有其他合法候選，正式派出的 job row 會帶
`dispatch_reroute = {"source": "executor-backoff", "skipped": [...]}` 收據，保留這次略過了哪個
identity 與其 `retry_after_epoch`。若全部候選都仍在 cooldown，Manager 直接回
`reason: executor-backoff` 與最早的 `retry_after_epoch`，不建立 worktree、job 或模型 session。

admission 前也會拿 registry 內既有 terminal job 當 caller inventory 與 store 對帳；若 store
unreadable/corrupt，或 inventory 與 store 還在 pending/conflict，Manager 會回
`reason: executor-backoff-unknown`，明確保留 unknown 而不是把它折成 allow/deny，也不捏造
`retry_after_epoch`；pending 終局由 admission 重播補 ack，unknown 診斷含 pending／replayed 計數與
replay diagnostics。修好 store 後重新 `resume` 即可；slice lane 的 admission 與 request／tick consumer 現在也會共用這份 durable state，透過 `dispatch_skipped_by_backoff` 明確區分 known cooldown 與 unknown（unknown 不代表 quota 已可用），但 #825 的 quota pool／forecast／reservation（R9）仍未完成。本票 merge 後才可再升級含 #928 的 pin。

### Planning capability probe boundary

Planning runtime 建構時，AGY capability probe 的 `build_agy_argv(...)` 若拋出一般 `Exception`，只會把 AGY probe 降為 `smoke-failed`／`ready=false`；這個局部 containment 不會把建構錯誤升格成整個 runtime failure。只要非 AGY primary 的 probe 成功，production planning runtime 仍可完成建構並保留 primary。失敗的 AGY identity 不會被選為 ready secondary；這只說明 probe 邊界，並不保證一定存在合格的異質 secondary planner。

這項邊界不涵蓋直接 launcher 的錯誤吞除：direct `SubprocessLauncher` 仍會讓無效 argv 設定向 caller 傳播，且在 argv 建構失敗後不啟動 Popen。真實非法 timeout env 的 direct／probe 雙路徑驗收仍屬後續 timeout child，不由本 child 冒稱完成。

### Planning 產出目的地綁定（#812）

Planning publication 對 `docs/superpowers/specs`／`docs/superpowers/plans` 現在只接受精確 stem 文法：spec／design 只能是 `<base>-spec.md`／`<base>-design.md`，plan 只能是 `<base>.md`／`<base>-plan.md`。`<base>` 只允許 `work_id`、`YYYY-MM-DD-<work_id>`，或從 `run.openspec_refs` 與同 work item 的 `docs/superpowers/workstreams/<slug>/todo.md` authority 推導出的 planning anchor slug，因此 change slug ≠ work_id 的 canonical 目的地仍可被接受。

combo manifest 既有的 `*<task-slug>*` outputs pattern 仍保留給 `openspec/changes/<change>/...` 使用，但不再單獨放行 `docs/superpowers/{specs,plans}`。這會同時關掉 substring prefix／suffix／middle／`-v2` 家族誤放行，以及「manifest pattern 命中但 kind 寫錯路徑」的 docs publication 入口；authority 重驗也與同一條 exact-stem 判準對齊。

### Quota observation schema boundary

`paulsha_cortex.coordinator.quota_observation` 是 #866 交付的純 schema／helper
模組：它只提供 standalone `UnitDefinition`、`PoolDescriptor`、
`ProfilePoolBinding`、`QuotaObservation` 的 bounded parser，以及
`binding_status()`、`freshness()`、`event_identity()` 三個 read-only helper。
這層目前**沒有**接到 workflow read model、delivery truth、或既有 usage pipeline；
production 路徑仍是 `registry.update_headless_result()` 先寫 terminal outcome，再交
`usage_extractors.extract_usage()` 擷取 usage，structured rate-limit／reset 訊號則
沿既有 `outcome_taxonomy.StreamEvidence` seam 保留。若未來要把 quota observation
餵進 source adapters、durable ledger、shadow read projection 或 admission，仍屬
#836 的後續 B/C/D child，不在這個 pure-schema owner 內偷接 consumer。

GitHub terminal closure scan 會以 authenticated default revision 的 Contents API 讀取 remote Todo，並重驗 path、blob SHA 與 base64 encoding；production 只對 canonical WorkflowRegistry 已連結的 PR 做 merge ancestry compare。只有 HTTP 502/503/504 會有限次 backoff retry，auth、rate-limit、其他 HTTP error、malformed JSON 或 identity mismatch 都立即保留 last-good 並標 degraded。

當同一個 `openspec:{repo}:{ref}` authority key 同時看到本機 `repo:{repo}` 的
`active` source 與 `github-terminal:{repo}` 的 `archived` source 時，claim 只在一個極窄條件下收斂成 archived：必須至少有一筆 confirmed `github_pr` source、每筆 PR source 的狀態都已是 `closed` 或 `merged`，且 `github-terminal:{repo}` 這份 **ok** snapshot 的 `observations.remote_prs` 對每筆 confirmed PR source 都恰好有一筆相同 `source_id` 的 row，並且 `merged_with_merge_commit is True`。缺 PR、terminal provider 缺席／degraded、`remote_prs` 缺席或型別不符、`source_id` 不匹配、同 `source_id` 重複，或任何其他 semantic conflict，一律維持原本的 authority conflict fail-closed，不做自動裁決。

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

### Builder 派工前 Todo admission

Manager 第一次派出 Builder 前會重新載入目前 WorkAuthority，要求 WorkAuthority-backed run 的 `mapped_todo_paths` 恰有一個 canonical workstream Todo，且 authority revision 必須與 `WorkflowRun.source_revision` 相同。Todo=0 時以 `builder-todo-missing` 停在 `needs_human`，提示發布 canonical Todo、link path、等待 Monitor 更新後再 resume；多個 Todo 才提示 unlink 多餘 mapping。若目前 authority 已變更，舊 run 不會繼續派工，需依既有正式重啟流程重新綁定。此 gate 在建立 Builder job、worktree 或 launcher 前執行，只攔 Builder；plan、verify、review 不受影響。Ship 的 Todo cardinality 檢查仍保留作 backstop，Todo=0 時也改提示補建與 link，避免錯誤建議 unlink。

## CLI

```bash
cortex list --repo owner/repo --state todo --explain
cortex work show unified-work-lifecycle --repo owner/repo --json
cortex work link unified-work-lifecycle --repo owner/repo --kind github_issue --ref owner/repo#14
cortex work unlink unified-work-lifecycle --repo owner/repo --kind github_issue --ref owner/repo#14
cortex work intake unified-work-lifecycle --repo owner/repo --issue 14
cortex work start unified-work-lifecycle --repo owner/repo
cortex work start unified-work-lifecycle --repo owner/repo --combo fix-standard
cortex work resume unified-work-lifecycle --repo owner/repo
cortex work retry-build unified-work-lifecycle --repo owner/repo --issue 14 --actor operator \
  --payload <(printf '%s\n' '{"expected_candidate":"<40-char SHA>"}')
cortex work abandon stale-canary --repo owner/repo --actor operator \
  --expected-run-id workflow-0123456789abcdef0123 \
  --reason 'Superseded by the terminal canary.'
cortex work reset-reclaim-budget stale-canary --repo owner/repo --actor operator \
  --reason '三代 abandon 皆肇因於已修復的引擎缺陷，非工作項本身'
cortex work refreeze-base long-lived-item --repo owner/repo --actor operator \
  --expected-run-id workflow-0123456789abcdef0123 \
  --reason 'main 已前進 13 支 PR，候選樹仍停在 claim 當下的基底'
cortex work auto unified-work-lifecycle --repo owner/repo --enable
cortex work auto unified-work-lifecycle --repo owner/repo --disable
cortex stat --combo-selections
cortex doctor --probe-live --repo owner/repo --json
```

### Combo 自動選擇

`cortex work start` 與 auto claim 建立 workflow 時，Manager 會先讀 durable snapshot 內已確認的 GitHub issue title，交給 `paulsha_cortex/deck/task_types.py` 的 taxonomy 做機械分類，再映射到 combo。現況 `feat` 會選 `feature-oneshot`、`fix` 會選 `fix-standard`；`docs`／`test`／`ci`／`refactor` 目前仍是明示缺口，會帶 `bypass-default` provenance 沿用既有 `feature-oneshot`。

若標題是 `unknown_type`、scope 不在受控詞典、或多個 mapped issue 得到互斥 type，claim 會 fail-closed，且不建立 WorkflowRun。修法只有兩種：修正 issue title，或用 `cortex work start <work_id> --repo <owner/repo> --combo <id>` 做 authoritative override。override 永遠優先於自動選牌，並會在 run 的 `combo_selection` 留下 `explicit-override` 來源。

`cortex stat --combo-selections` 會彙總 `source × task_type`，直接看出多少 run 是自動選牌、多少走 override、多少因 title 缺席／unparseable／combo 缺口而 bypass。`fix-standard` 雖然比 comment 草稿多了 `openspec-propose` 與 `writing-plans` 兩張 planner 卡，但這是為了滿足 `validate_manager_spine` 的完整 phase spine；verification 與 code-review 兩條核心 gate 維持不變。

#### 自訂 combo（instance-local override）

`paulsha_cortex/deck/schema.py` 的 `resolve_combo_path()`／`iter_combo_files()`（`deck/cli.py`、`work_bridge.py`、`porcelain/init_sample.py` 皆已改走這兩個入口）會先查 `$PSC_AGENTS_ROOT/config/combos/<id>.yaml`，找不到才 fallback 到套件內建 `paulsha_cortex/deck/data/combos/`。同 id 時 instance-local 優先於套件內建，且 reinstall／升級套件不會蓋掉這份自訂檔——把自訂 combo YAML 放進 `$PSC_AGENTS_ROOT/config/combos/` 即可長期覆寫或新增 combo，不需要 fork 套件內建資料。兩個目錄都找不到指定 id 時 fail-closed，錯誤訊息會列出實際搜尋過的目錄清單。

`small-fix` 是套件內建的輕量 combo 參考實作（`workflow-claim → brainstorming → writing-plans-light → subagent-build → verification → code-review → policy-commit`，7 張卡、2 條核心 gate_spine），刻意用 `writing-plans-light`（只吃 `docs/superpowers/specs/*<task-slug>*-design.md`，不依賴 `openspec/changes/<change>/proposal.md`）取代 `writing-plans`，打斷小任務不需要的 openspec 全鏈。`small-fix` 只能經 `--combo small-fix` explicit override 使用，不在 `task-types.yaml` 的自動選牌映射中（`combo.task_type` 填 `small-fix`，不是 `fix`——避免和 `fix-standard` 的自動選牌搶同一個 `fix` task type）。

### Sizing 評分與 stability-risk-v2

五個 sizing 維度各為 0–2 分，總分仍為 0–10；Green／Yellow／Red 門檻仍分別是 0–3、4–6、7–10。`domain_breadth`、`state_consistency`、`acceptance_surfaces` 與 `orchestration` 的計算不變，只有 `spec_stability` 以風險方向的 `STABILITY_RISK_ALGORITHM = "stability-risk-v2"` 識別：

| completeness 情況 | stability risk |
|---|---:|
| 三件 accepted 材料完整、無 blocking marker | 0 |
| 恰好缺一個 kind，且沒有拒收或阻塞 | 1 |
| 缺至少兩個 kind、任一 blocking marker，或存在未 accepted artifact | 2 |

空白、unknown 或彼此不一致的 completeness report 也保守給 2；缺少或 invalid 的 plan `domain_breadth`／`state_consistency` 仍使純函式拋 `ValueError`，既有 `current_sizing_snapshot` 則維持 `(None, None)` fail-soft。較低的 stability risk 不代表 planning 已 accepted，也不會略過 readiness 或其他 gate。

此映射只由新的 claim／reclaim 與既有明示 retry 重算入口採用。讀取或重啟不會重算、改寫舊 WorkflowRun、frozen planning、CompletionRecord 或 immutable evidence；沒有可辨識算法來源的歷史分數保留為 legacy／unversioned，不以目前 runtime 猜填。需要新分數時，必須走正式的明示重新評估流程並以當時實際載入的 runtime revision 留下紀錄。

### Intake（`link` + `start` 合成，#203）

`cortex work intake <work_id> --repo <owner/repo>` 是「拿到一個 issue/task 就進件」的單一入口，取代已停用的低階 `dispatch`。它等價於「（必要時）`link` 後接 `start`」，但收斂成一次呼叫：

- 帶 `--issue N`（或 `--kind/--ref`）時，若該來源尚未反映在受監控快照的 `mapped_issues`／`mapped_openspec`／`mapped_todo_paths`，會先寫一筆 override link（與 `cortex work link` 相同語法、相同 fail-closed 驗證），再重新載入 authority。
- 省略 `--issue`／`--kind`／`--ref` 時，直接沿用 work_id 現有的 confirmed authority（等價於單獨呼叫 `start`）——這是「work_id 已有 confirmed Todo 或已 link issue」時的常見用法。
- 兩種路徑最終都轉交既有 `start` 語意（`claim_key` 去重、`--combo` override 皆比照 `start`），不繞過 `default_workflow_manifest`／`validate_manager_spine`。
- **Intake 不會憑空建立新 authority**：`.cortex/work-items.yaml` 這份 override 檔與受監控的 `work-items.snapshot.json` 是兩份分開維護的狀態，override 寫入後仍要等下一輪 Monitor correlation 才會併入快照。因此若 work_id 既無 confirmed Todo、也未曾 link 過 issue，且本次呼叫也沒有帶 `--issue`／`--kind`/`--ref`，intake 會 fail-closed 拒絕，不建立 WorkflowRun；純文字任務仍需要先有明文授權來源（confirmed Todo 或 linked issue）才能進件。
- **只 link issue 不足以讓 work item 變成可 claim**（#389）：`intake --issue N` 成功寫入 link 後，work item 仍停在四態 read model 的 `topic`（見上方「四態 read model」），因為 lifecycle reducer 只在 `active_todo`（存在 `todo`／`superpowers_spec`／`superpowers_plan`／`openspec` 其中一種 active 來源）成立時才會前進到 `todo`，而只有 `todo` 狀態才會投影出 `start` next_action。換句話說，claim 的完整前置條件是「confirmed Todo 來源」，**不是**「confirmed Todo 或 linked issue」二選一——issue 只負責把 work item 從無到有變出來（`topic`），要能被 claim 還需要另外用 `cortex work link <work_id> --repo <owner/repo> --kind todo --ref <path/to/todo.md>`（或 openspec/spec-plan）補上一個 active 的 Todo 來源。對只連結 issue、沒有 Todo 來源的 work item 呼叫 `intake`／`start` 會 fail-closed，錯誤訊息會標示該 work item 目前所在的 lifecycle state 與缺少的 Todo 來源（`reason=authority-not-startable` 或 `authority-no-confirmed-todo-source`），不再與「work_id 完全不存在」共用同一句泛化訊息。

```bash
cortex work intake unified-work-lifecycle --repo owner/repo --issue 14
cortex work intake unified-work-lifecycle --repo owner/repo
cortex work intake unified-work-lifecycle --repo owner/repo --issue 14 --combo fix-standard
```

Telegram 等 bot 宿主若要提供「貼一段文字/issue 就進件」的入口，應呼叫 `submit_work_action(action="intake", ...)`（`paulsha_cortex/control/client.py`）；既有的 `/dispatch <slice_id>` 走既存 slice_id 派工，維持原樣不變，不在本次範圍內改動。

### Headless launcher session boundary（#823）

Headless job 的每次合法 `Popen`（direct、`systemd-run`、`systemd-template` 的外層
Manager client，以及只移除 `stdin` 的相容 retry）都必須使用
`start_new_session=True`。direct mode 的 child 因此離開 Manager 的 POSIX session/process
group；這不等於把程序移入 systemd cgroup，也不承諾 Manager daemon restart 後 job 存活，
更不改變 systemd unit 的 cgroup／`KillMode` 語意。#824 的 timeout/parser/CLI 合約與
#851 的 AGY probe containment 仍由各自 work item 負責；#823 不藉 session flag 宣稱
timeout、cancel、probe 或 issue closure 已完成。

### AGY print timeout boundary（#824）

AGY 的 headless `--print` 形態現在一律帶單一 `--print-timeout <Ns>`。若 caller 明示
`PSC_AGY_PRINT_TIMEOUT`，launcher 先做 `strip()`，只接受 ASCII digits，容許前導零但會
正規化成 canonical `Ns`；空白、零、符號、小數、Unicode digits、已帶 `s` unit 或超過
Go `time.Duration` 整秒上界 `9223372036s` 都會在 spawn 前 `ValueError`。`build_agy_argv`
的顯式 `print_timeout=` keyword 只接受已 canonical 的 `Ns` 字串，不做 strip 或前導零修正。

未設定 `PSC_AGY_PRINT_TIMEOUT` 時，launcher 直接重用既有
`gate_ledger._gate_timeout(env)`：`max(DEFAULT_GATE_TIMEOUT_SECONDS, gate_seconds) + 600`，
最後才檢查 AGY/Go 上界。這保留 `PSC_GATE_TIMEOUT` 對非法／非正值的既有 fallback，不新增第二份
gate parser，也不改變 planning runtime 的 45 秒 probe process deadline、120 秒 planning
timeout，或其他 executor 的 argv 形狀。

### Work identity migration（設計中，見 ADR-0002）

`link`／`unlink` 目前一次只能對單一 `(work_id, source)` pair 生效，重識別
（例如 `-v2` 世代熔斷）要把一批來源整批從舊 work_id 搬到新 work_id 時，只能
靠多次分開的 `link`／`unlink` 呼叫加上「等 Monitor 下一次 rescan 確認生效」
的手動判斷——`hamanpaul/paulsha-cortex#326`–`#330` 是本專案自己實際跑過一次
的完整記錄，橫跨 5 個 PR、近 9 小時。`docs/adr/0002-work-identity-migration.md`
定了收斂成單一 `cortex work migrate` 動詞的設計（單一 atomic override
transaction、凍結 authority 做 abandon CAS、不放寬 `claim.py` 既有的碰撞
不變量），供後續 code 票直接實作；本文件的 `## CLI` 範例區塊會在該動詞落地
後同步補上。

`retry-build` payload只接受`{"expected_candidate":"<40-char SHA>"}`。Manager會把它當CAS，不把caller內容當evidence；通常只有ongoing `needs_human` verify/review run、無active job、舊build全passed且Candidate完全相同時，才原子重開最後一張builder card，清除舊verify/review authority並立刻派出新builder。另一個窄化入口只處理final builder terminalization失敗：run必須停在build phase、前置build card全passed、final card pending，而且最新同card job已成功退出（`exited/0`）卻沒有workflow evidence；真正的failed job不符合此入口。所有recovery prompt都要求先檢查worktree是否已有repair commit，並允許builder提交或採用已測試的descendant Candidate；Manager仍獨立驗證exact舊Candidate CAS與單調ancestry。terminalization recovery另要求保留declared input snapshot並先檢查未綁定commit。Plan/build terminal的`outputs`只可列出符合manifest的repo-relative artifact paths；manifest沒有outputs時必須精確回`[]`，不得塞入action/summary物件。Ship authority 原則上必須仍為pending；唯一例外是已通過且 identity 精確為 `cortex-manager/deterministic/cortex` 的 `openspec-archive`，此時保留official archive step並只重設後續gate，讓post-archive finding可由tested descendant Candidate修正；對未宣告 `openspec-archive` step（如 `fix-standard`）的 combo，archive-applied 以恰好一筆 Manager archive job ship evidence 加上 `subject_head == candidate` 或 Git ancestry 判定。若唯一 mapped OpenSpec change 的 exact Candidate Git tree 同時含 active change 與 matching archive entry，`retry-build` 仍會派 builder，但 action response 會明列 warning 要求下一次 review 前修正並存。Manager會把已移走的active brainstorm artifact對應到同hash且唯一的official archive path重證，不接受caller改寫authority、模糊archive或symlink；任何其他已通過ship card仍拒絕retry。新Candidate仍必須是舊Candidate的exact descendant。`link`、`unlink`、`start` 與 `resume` 不要求 caller 提供 repo root；Manager 只會從 installer 的 `PSC_REPO_ROOT` 或 Monitor workspace registry 解析與 `owner/repo` remote 完全一致的 canonical git top-level。當同work只有一個`done/ship` run且terminal journal binding完整時，explicit `resume`會重跑current authority的ship validator來刷新stale CompletionRecord；不會建立新run、重開builder或dispatch card，pending／needs-human／malformed結果也不會覆寫既有completion。`auto` 未指定相容用的 `--issue` 時會套用到全部 confirmed mapped issues。

`recover-repair-commit`（#260）處理另一種 build phase 卡死：repair job 以 `status == "failed"`、exited 且 exit code 非 0，或 exited/0 但 terminal payload 缺漏／malformed 終止，卻已在既有 builder worktree 留下合法 descendant commit。此窄化入口只在 run 為 ongoing、帶 `needs_human`、停在 build phase、前置 build card 全 passed、final builder card pending、最新同 card job 無 bound `workflow_evidence` 且無 active job 時可用，且不啟動任何 model session——adoption 完全由 Manager 側確定性驗證完成。判準全部取自系統事實：worktree 路徑取自該 failed job row（不接受 caller 指定路徑）；operator 提供的 `expected_run_id`＋`expected_candidate`（40-char SHA）只做交叉比對——`expected_candidate` 必須精確等於該 worktree的 `git rev-parse HEAD`、worktree 必須乾淨（`git status --porcelain` 為空）、必須為原 candidate 的合法 descendant（`git merge-base --is-ancestor`）且不得與原 candidate 相同；任一不符即 fail closed 並回報具體原因，candidate authority 不變。成功後會寫入一筆 immutable `cortex-work-repair-adoption/v1` evidence record（含 failed job id、observed HEAD、adopted／previous candidate、actor），並登錄一筆沿用既有欄位集合的 adoption job row：identity（executor／model／independence_domain）、worktree 與 dispatch_head 複製自 failed job，`subject_head` 為 adopted candidate，狀態 exited/0，`workflow_evidence` 指向該 record；failed job 原始 row 原樣保留不被改寫。Manager 接著原子完成 `candidate_head` 換綁、final build card 標 passed（以 adoption job 的 builder identity）、`current_phase` 進 verify，讓既有 verify → foreign review → exact-head final 管線對 adopted candidate 重新把關，不重跑已完成的 planning。重送相同 request（`candidate_head` 已是 expected SHA 且對應 record 存在）回報 `already-recovered`，不產生第二次 adoption、第二個 job row 或任何 model session。此 action 與 `retry-build` 的 exited/0 unbound 窄化入口對同一情境刻意重疊——两个入口都合法，由 operator 依 commit 是否可信選擇；两者 CAS 各自獨立，不互相放寬，`retry-build` 的 exact `expected_candidate` CAS 與既有窄化入口行為維持原封不動。periodic runner 不取得此 recovery authority。

`resume`／`retry-build` 的 job 選擇同樣於 #260 收斂：operator resume 遇到已 terminalized 的失敗 job 時（`status == "failed"`，或 `status == "exited"` 且 exit code 非 0），第一次 `cortex work resume` 即 dispatch replacement job，不再重選 stale failed job 空轉一輪（過去只認 `status == "failed"`，`exited` 非 0 的 stale terminal 會讓第一次 resume 只重新回報 `job-failed`、要再執行一次才 dispatch replacement）；exited/0 的既有三條路徑（unbound terminal recovery、malformed schema retry、正常 terminalize）條件式不動。replacement dispatch 後再次 resume 回報 in-flight，不產生第二個 replacement job。失敗回報一律附掛 `_terminal_parse_diagnostics` 的唯讀 `terminal_diagnostics`（observed HEAD、job id、失敗原因），與既有的 `authority_granted: false` 模型一致：可觀測不等於可授權，不會因此讓 candidate 取得任何 authority。

`recover-pre-candidate`（#547，吸收 #968–#971）現在由 Manager slice action 與 work action 共用同一 recovery core。work action 只以已驗證 WorkAuthority 的 repo／Work Item 查找唯一持久 `owner_identity={repo,work_id,slice_id}`；不看 slice 名稱、spec suffix、branch 或 registry 順序。兩入口都要求 slice 與 builder job 的 owner identity、attempt id 相符；工作區仍存在時，marker 也必須帶相同 tuple。缺身分、legacy unbound、歧義、job／marker attempt 不一致、回收失敗或 manifest read-back 不符時，拒絕回報成功；成功時在 registry 一次寫入 pending transition、action/history 與 binding 清除，再 supersede handoff manifest 並核對狀態。新 slice spec 可明示 `repo` 與 `work_id`，派工在建立工作區前固定 attempt id，並將同一 tuple 寫入 slice、job 與 workspace marker；缺少這兩個 metadata 欄位的 row 不會自動升級。

`#862` 仍只提供 registry 內部原語：`lookup_registry_request_receipt()`、`prepare_recovery()`、`commit_pre_candidate_recovery()`、`record_job_supersession()`、`record_job_consumption()` 與 `checkpoint_legacy_binding()`。這些不是新的 public CLI 動詞；本次 recovery 沿用現有 action/history 與 handoff 契約，不新增第二套 receipt／CAS。直接使用 #862 recovery primitive 的 caller 仍須在 allowed-action gate 內提供 exact control pins——`target={repo,work_id,slice_id}`、完整 binding snapshot（含 `binding_version`／`binding_revision`／builder/reviewer refs／spec/plan hash／verification hash／target remote/branch／dispatch base）、非空 `required_steps`、immutable `request_id`／`request_digest` 與 proof refs；A 只驗 schema、digest、CAS 與 required-step closure，不會憑 `actor`／`requested_by` 或 repo 字串授權，不讀外部 proof ref，也不替 queue/done、CompletionRecord 或 terminal lifecycle 做 reconciliation。

`checkpoint_legacy_binding()` 同樣是 registry-only primitive：caller 必須以**另一個** immutable request ID/digest，帶完整 observed legacy row、legacy binding projection、job refs、fingerprint 與 provenance 明示觀測。成功時它只把這次觀測定錨成 `binding_revision=1` 的新起點，保留既有 candidate/history/actions/job disposition，不清 binding、不自動連帶 recovery。它只證明「觀測當下的 row 與 commit 當下相符」，**不是** legacy 歷史 A→B→A 的 ABA 追證，也不是 done/ship 權威；本次 #547 recovery 不呼叫這個 primitive，已存在的 unbound row 即使有 checkpoint 也不會自動遷移或回收。

`abandon`只處理尚未進入delivery的舊run：exact run CAS、current WorkAuthority refs、actor與單行reason全部重驗，且任何active Job、PR ref、passed ship step或CompletionRecord都會拒絕。成功後把該run標成`superseded`，並以immutable `cortex-work-abandon/v1` evidence保存reason；不勾未完成tasks、不建立CompletionRecord，也不把abandoned work投影成done。終態化後會立即對該run reconcile planning transaction，並 best-effort 回收未提交 planning artifact 與 build worktree；build branch 若有超出其 base 的 commit，先建立 `archive/<work_id>-<shortsha>` tag 保留，再刪 branch，沒有額外 commit 就直接刪除。重送同一CAS/reason冪等，不同reason或已有另一個active run則fail-closed。

`reset-reclaim-budget`（#519）是語意 re-claim 世代熔斷（#218 AC2）的唯一解鎖路徑。同一 `(repo, work_id)` 累積 `SEMANTIC_RECLAIM_LIMIT`（3）個 superseded 世代後，`start`／auto-claim 都會停在 `needs_human: semantic-reclaim-budget-exhausted`；熔斷的計數對全部歷史累加，不看時間窗也不看失敗原因，因此當三次 abandon 全肇因於 cortex 自身缺陷（而非工作項本身）時，缺陷修好之後 work item 仍會永久鎖死。此 action 要求 `--actor` 與單行 `--reason`（界限與 `abandon`／`retire-delivered` 完全相同：actor ≤128 字、reason ≤500 字、單行、可列印），不需要 `--expected-run-id`——熔斷觸發的前提就是沒有 active run 可供 CAS。重置以 **append-only 水位**實作：把當下所有未赦免的 superseded `run_id` 記成一筆 registry 授權列（狀態檔的 `reclaim_resets` 根欄位），熔斷計數改為「superseded 世代扣掉所有已赦免 run_id」。既有 WorkflowRun row 一列不刪不改——run 歷史是稽核來源，重置是新增一筆授權事實，不是抹掉失敗紀錄。水位以 run_id 集合而非時間戳表達，因此重置後新產生的 superseded 世代照常累加、熔斷會再次上膛，不是永久關閉安全機制。每次重置寫入一筆 immutable `cortex-work-reclaim-reset/v1` evidence（`repo`／`work_id`／`actor`／`reason`／重置前的世代數與其 run_id 清單／`created_at`），落在 `<coordinator_root>/evidence/work-reclaim-reset/{work_id}-{hash}.json`，命名與原子寫入慣例比照 `cortex-work-abandon/v1`。沒有任何可赦免世代時 fail-closed 拒絕（已重置過則回報 `already_reset`，不寫第二筆授權）。熔斷本身的 `needs_human` 結果也會回報 `legal_next_steps`／`next_step_hint`，直接指出這條解鎖路徑與所需的理由參數。`reclaim_resets` 是加法相容的可選根欄位（不 bump `schema_version`），本欄位出現前寫下的既有狀態檔照常載入，缺欄位一律視為沒有任何重置授權。

`refreeze-base`（#731 (A)）是**候選 git base 的重新凍結入口**。候選基底的權威來源只有一處：`manager._dispatch_workflow_card` 建**首張 build 卡**工作區時讀的 `WorkflowRun.frozen_readiness["base_sha"]`，它一路傳進 `seams.ScriptWorktreeCreator.create(..., base_sha=…)`；該欄位為 `None` 時 `create()` 退回 `self._base`，而 dispatch 建 creator 時傳的是字面 `base="main"`，因此實際基底變成**來源樹的本地 `refs/heads/main`**（沒有人推進它，`git fetch` 只動 `refs/remotes/origin/main`）。凍結本身是刻意且正確的（hermetic pinning，#211／#208 A.2），缺的是重新凍結——`work start` 對還有 active workflow 的 work item 回 `action=resume / reason=active-workflow`，不走新 claim，所以 `abandon` ＋ `reset-reclaim-budget` ＋ `start` 連續換代一次都換不掉基底（0819 實測：mirror 已是新 SHA，候選樹仍停在舊 SHA，導致已在 main 的修法結構上進不了長壽 run）。

此 action 要求 `--expected-run-id`（exact WorkflowRun CAS）＋ `--actor` ＋單行 `--reason`（界限與 `abandon`／`retire-delivered`／`reset-reclaim-budget` 完全相同：actor ≤128 字、reason ≤500 字、單行、可列印，由 `control/contract.py` 在所有入口的收斂點強制）。`origin/main` 的取得走 claim 用的**同一支** `claim_readiness.base_sha_probe`，不另寫一次 fetch。

入場條件全部 fail-closed，任一不成立即拒絕且不留 side effect：run 必須是該 `(repo, work_id)` 唯一的 `ongoing` canonical run 且 run id 精確吻合；`current_phase` 必須在 `claim`／`define`／`plan`／`build`（基底只在 build 卡 provisioning 被消費）；`candidate_head`／`verified_head` 必須皆為 `None`（一旦有被採信的 build 成果，下一張卡的 base 改由 `_workflow_build_handoff_base()` 決定，重新凍結會是靜默 no-op）；不得有 `dispatched`／`running` 的 job；不得有已發佈交付物（`pr_refs`／`pr_candidate`／`merge_revision`）。此外新基底必須是**每一條已記錄基準**的後代或相等——基準集合為目前凍結值（未凍結時取來源樹的本地 `main`）∪ 本 run 每個 job 的 `dispatch_head` ∪ build branch 現在的位置——非 fast-forward 一律拒絕，不是「以最新的為準」。其中 build branch 那一條就是 **#613**（abandon 不回收 build branch）的前置檢查：branch 上若還有新基底以外的 commit，下一拍 provision 必定撞 `existing worktree branch has commits outside requested base`，因此在**改任何狀態之前**就以與 `ScriptWorktreeCreator.create()` 相同的 `git merge-base --is-ancestor` 述詞拒絕，不製造「refreeze 成功、下一拍才炸」。

成功時寫入一筆 immutable `cortex-work-candidate-base-refreeze/v1` evidence（`repo`／`work_id`／`run_id`／`actor`／`reason`／`previous_base_sha` ＋ 其來源／`base_sha`／mirror 的 `remote_fetch` 結果／全部 fast-forward 基準／build branch 與其位置／重新凍結前的 phase 與 facets／`created_at`），落在 `<coordinator_root>/evidence/work-candidate-base-refreeze/{run_id}-{hash}.json`，命名與原子寫入慣例比照 `cortex-work-abandon/v1`；evidence ref 一併 append 到 run 的 `evidence_refs`。run 已有凍結集時**逐欄保留**、只換 `base_sha`（其餘欄位是當初那次 readiness transaction 的產物，這次沒有重跑，不得順手覆寫）；沒有凍結集時寫入一個 `cortex-candidate-base-freeze/v1` 的最小凍結記錄——刻意不假裝成 `pre-claim-readiness-frozen-set/v1`，那個 schema 的語意是「六道 readiness 關卡都通過了」。基底已經凍結在同一個值時回報 `already_current`，不寫第二筆 evidence。

**出口狀態 == 入口狀態**（與 #728／PR #729 同一條紀律）：本 action 不動 `current_phase`、不動 `facets`、不動 `candidate_head`、不動任何 step 的 `gate_result`，唯一的狀態變更是 `frozen_readiness["base_sha"]` 與 append 一筆 evidence ref，因此重新凍結後的 run 狀態就是重新凍結**之前**那個狀態，結構上不可能不是後續每一拍的合法入口狀態。推進仍由既有出口負責（run 停在 `needs_human` 時回傳會附上 `next_actions`，例如 `retry-card`；`retry-card` 之後由既有 `force_new_card` dispatch 派出新 job，新工作區的 `git rev-parse HEAD` 即等於新基底）。**hermetic pinning 一個位元組都沒有放寬**：重新凍結之後 `origin/main` 再前進，候選樹不會跟著漂。

若 delivery 尚未建立 immutable binding，就因 PR／Todo target 不是各一個，或 OpenSpec target 多於一個，而停在 `needs_human: multiple-delivery-targets-unsupported`；`mapped_openspec == ()` 本身是合法 ship 模式。operator 修正 repo-local correlation 後可明確 `resume` 同一 WorkflowRun。Manager 只會在 current authority 已重新收斂為恰好一組 target 時清除此特定 stop；已建立 binding 或其他 `needs_human` 原因仍維持 fail-closed。

工作啟動後，`$PSC_COORDINATOR_ROOT/jobs.json` 內的 `workflows` 是唯一 workflow lifecycle truth。整份 registry 以 exact durable-byte SHA-256 revision 加 canonical `jobs.json.transaction.lock` sidecar 做 compare-and-persist：任何 request／tick／workflow mutation 若拿著 stale revision 進來，都會在寫入前被明確拒絕為 `RegistryRevisionConflict`，並把 registry memory 完整重載回當前 durable snapshot，而不是自動 merge／retry／replay。經 control queue 消費的 request 若撞到這類衝突，也會先把 explicit `done` error（含 expected/actual revision 與 canonical path）durably 寫出，再移除 request file。Delivery journal 只保存以同一 `run_id` 為 key 的 resumable ship phase，不另建 lifecycle state。ship transition 現固定分成 `local-closeout → pr-preflight → external-ship` 三段：沒有既有 PR 時，Manager 先在 builder worktree 完成本地 closeout（canonical report cleanup、official `openspec archive`、archive commit 與 candidate reset 回 verify），此段零 `gh`／`git push`；若 `mapped_openspec == ()`，local-closeout 會跳過 official archive，remote facts 也把 archive 視為 not-required。若 `openspec archive` 的 stdout/stderr 含 `Aborted`，或 archive 後 active change 沒有搬到 matching `openspec/changes/archive/<entry>/...`，Manager 會在 archive commit 前直接 fail-closed，不產生 archive commit；若 post-archive Candidate 又同時帶回 active change 與 matching official archive，review→ship advance 也會用同一條 fail-closed backstop 擋下。closeout 完成後才會在乾淨、policy-compliant且完成後刪除的暫存 `feature/preflight-*` exact-Candidate checkout 以 metadata context 跑 PR preflight。preflight 失敗會停在可 resume 的 `pr-preflight-blocked` typed stop，本地 closeout 結果保留、run 可直接 resume 重試。preflight 通過後 Manager 接著 push exact Candidate、冪等呼叫 `create_or_get_pull_request` 建立 PR，並把 `pr_ref` 原子寫回同一個 `WorkflowRun`——沿用既有 operator authorization 模型（push／PR 建立不需額外手動授權，merge 仍受 `merge_authorization`／operator `resume` 把關）。既有 PR 的 push、metadata 寫入、review request 與 merge 仍全部留在 external-ship 段；builder worktree 內的 accepted planning overlay 不會混入這條 exact-tree gate。Monitor 將 source membership 更新納入 WorkAuthority 時，只有新增項目能由同 run 已接受的 planning artifact ref/kind/hash 與目前 bytes 證明，或是 `pr_refs` 對應同一 exact verified Candidate 的單一 Manager PR，claim/reconciliation 才沿用原 claim-era 並保留 gate、evidence 與 attempts；source 對不上或 issue／OpenSpec／Todo 等其他 authority 欄位改變，仍走既有 restart。PR 建立只更新 `pr_refs`，保持 `source_revision`／claim key 為 claim-era；delivery journal 仍依 current authority 更新。Canonical Job envelope持續以Job dispatch時保存的immutable source revision重驗，不會因run current revision前進而改寫或誤判舊證據。slice-based foreign review worktree 與 workflow reviewer sandbox 兩條路徑都會 materialize frozen authority refs，逐檔重算 sha256 驗證；缺檔、hash drift 或未紀錄的 overlay 一律 fail-closed。reviewer terminal 若缺席 `authority_hashes`，Manager 只會用同一份 pinned input snapshot 補齊；模型一旦有帶值，仍必須逐字相符，任何 drift 一律拒收。Manager啟動quick policy與configured CI-parity gate時會移除所有繼承的`PSC_*` runtime authority，並改用完成後刪除的disposable `HOME`／`XDG_CACHE_HOME`；Python user-site與GitHub config等必要工具／認證root則顯式保留，避免preflight測試經由installed bootstrap重新取得production coordinator、executor或repo。Manager systemd unit固定`UMask=0022`，讓exact-Candidate suite不受operator service umask影響。Verify/review report是Manager-owned evidence material：最後一張review已取得immutable canonical evidence後，delivery只會清除hash完全吻合且未被Candidate追蹤的report，並在刪除前寫入hash-addressed immutable cleanup intent；只有同一intent的crash/retry evidence reader可接受report已不存在，unknown、tracked、symlink、可寫或malformed intent、未授權缺檔或drift一律阻擋。若review-complete run的ship validator失敗，Manager會先持久化`needs_human`與failed gate再回報錯誤。`mapped_openspec > 1` 仍維持 `multiple-delivery-targets-unsupported` fail-closed，需先用 `cortex work unlink` 修正 correlation 後再 `resume`。後續 merge 與 CompletionRecord 也綁定該 run 的 exact Candidate 與 canonical verification/review evidence。Yellow plan review 只有在結果明確 ready 後，才會以單一 registry transition 將被接受的逐檔 bytes/hash 與 immutable planning source revision 寫入 receipt 並同步 run baseline。若 verify dispatch 前發生 planning drift，Manager 會保存 exact candidate/card/hash stop；operator resume 只有在尚無 verify job、receipt 綁定仍有效且既有 candidate workspace 逐檔符合 review receipt 時，才會以 CAS 更新 baseline 後續跑同一 candidate，原有 checkbox-only 容忍維持不變。

自 #983 起，`delivery-journal.json` 的所有 Manager 寫入都必須經過 `work_actions.py` 內同一條 conditional-commit boundary：writer 先拿固定 sibling lock，再用 `exists + persisted revision + raw-byte digest` baseline 比對 current durable state，只有完全相符才允許 full-file commit。成功提交會 fsync temp、`os.replace`、fsync parent dir，並在 lock 內 fresh reread exact payload 後才算 committed；baseline 漂移、直接覆寫另一個 writer 的 row、或 ordinary `_save_runs` 想新增／刪除／替換 publication entry 都會 fail-closed。對 narrow publication append API，outcome 明確分成 `committed`／`conflict`／`unknown`：`unknown` 不授權後續 PR side effect 或 receipt minting，必須 fresh locked reread 同一 event identity 才能判定 exact replay 是否其實已落地。

既有 PR metadata transaction中的 title/body PATCH、labels PUT及PR/issue identity reread，只有在明確 HTTP 502/503/504 時做有限次 backoff retry；每次成功仍須完整reread。PR create、Candidate push、review request、merge與其他 delivery side effect不套用這個 retry，auth、rate-limit、其他 HTTP error 或 malformed response 立即 fail-closed。

Manager在metadata write前先authenticated reread PR title/body與完整labels；若三者已精確符合canonical metadata，就不發PATCH/PUT。只有確認drift才執行冪等write，之後再完整reread；因此write omission仍是有remote evidence的validated no-op，不是跳過gate。

Verify/Review dispatch只接受schema v2明示`review` capability、且independence domain不同於會產出 Candidate commit 的 build card 的identity；`commit_policy=forbidden` 的隔離確認卡不計入 builder domain。Reviewer以enforced read-only mode在exact Candidate的disposable clone執行；Claude reviewer固定使用`dontAsk`與`safe-mode`而非Plan Mode，只暴露OS-sandboxed Bash，並由Manager-generated phase contract把StructuredOutput收緊成verification或review exact schema，不載入Candidate customization、remote session或MCP。Filesystem拒讀home、`/run/user`與Docker sockets；Linux/WSL會先解析並去重`/run`、`/var/run`等symlink aliases，避免同一socket形成衝突bind，仍只重開Candidate、Python user-site工具鏈與解析後的官方SRT package root（供`apply-seccomp` helper執行），並以`failIfUnavailable`、禁止unsandboxed fallback及Candidate deny-write執行測試；review subprocess只保留非密鑰基礎環境且使用非login shell，避免parent env或shell profile匯入credentials。Linux/WSL缺Claude Code 2.1.187+、必要CLI surface、`bubblewrap`、`socat`或`srt`，或live native/configured-policy/Unix-socket seccomp smoke失敗即fail-closed。Manager把Claude protected-path bind targets建立在deterministic disposable session root，exact Candidate固定置於其`candidate/` checkout，避免污染Candidate material tree；terminal、launch failure與operator retry路徑都會重驗原Candidate完整tree snapshot後清除整個session root。terminal只回substantive verification/findings與inline Markdown body；Manager依durable Job自行建立report frontmatter、Candidate/job/identity binding與GateEvaluation。Report路徑限於phase專屬的`reports/verify/*.md`／`reports/review/*.md`，durable publication journal可在多檔partial write、canonical evidence或registry save fault後rollback，亦可在已bind的crash replay中roll-forward。整份log恰為單一JSON fenced object時可解析，但含prose、第二個fence或錯誤schema仍fail-closed。

舊版曾把 planning-only canonical Agy 誤派成 reviewer，亦曾把Claude reviewer啟動在Plan Mode而得到`exited-0`卻沒有terminal payload。這些既存 terminal 不會成為 evidence；只有 operator 明確執行 `cortex work resume`，且最新 Job 的 run/claim/repo/source/card/phase/Candidate/builder/reviewer identity/output/sandbox snapshot contract 全部精確吻合時，Manager 才保留舊 Job/log並重派一次。Reviewer的原始Candidate root必須精確等於已驗證Builder Job worktree，而不是WorkflowRun主workspace。Periodic runner 不取得此 recovery authority。

工作預設 manual。Auto claim 同時要求 confirmed Todo、confirmed issue 與 `cortex:auto-on-going` label；移除 label 只阻止尚未 claim 的工作，不會中止 active workflow。Todo 缺 issue 時不會自動建立 issue。

**缺 issue 不建立 run（#669）。** 判定 `missing_issue` 時，claim 回 `action: not_claimable`、`run: None`，**不呼叫 workflow starter**。舊行為是「先建 run 再宣告 blocked」（run 的理由逐字寫著「claim 判定需要人工介入即建立 run」），於是 `docs/superpowers/workstreams/*` 這類**設計上就不對應單一 issue** 的 work item，每一個都被物化成停在 `current_phase: claim`、`gate_state: running`、`evidence_refs: []`、`next_actions: []` 的 `needs_human` run——實測首輪掃描一次產出 24 個，永遠不會推進，把 `attention` 的信噪比壓成 1:24。`missing_issue` 對這類 work item 是**預期狀態，不是異常**，不該進入 durable 的 run 生命週期。

但「不建 run」不得等於「靜默略過」，否則真的漏開 issue 的 work item 會變成盲區（fail-loud 換成 fail-silent）。每一次跳過都會在 `<coordinator_root>/not-claimable.json`（schema `cortex-not-claimable/v1`）記一筆：`reason`／`detail`／`source`、`first_observed_at`／`last_observed_at`／`observations`（卡多久、被判過幾次）與可照抄的 `next_step_hint`；`cortex status` 以獨立的 `not_claimable` 區塊呈現（`attention` 因此只留可行動的項目），`cortex digest` 帶計數。work item 一旦重新可 claim（補了 issue、或既有殭屍 run 已被 abandon），下一次判定即自動清掉該筆，不留永久假警報。

修正**不會自行清除**修正前建立的殭屍 run（沿用「auto-claim 不得自動清除或重試 `needs_human` run」的守衛）。這類 run 有唯一可機械辨識的簽名——ongoing ＋ 停在 `claim` phase ＋ 掛 `needs_human` ＋ 結構化理由為 `claim-blocked`／`work_bridge.start_workflow_for_authority` ＋ 零 evidence 與 PR——命中時 claim 回 `reason: claim-blocked-stale-run`、附上該 `run_id` 與完整的 `cortex work abandon … --expected-run-id …` 指令，由 operator 明示清除。少任何一項簽名都不算殭屍，停在 build／verify／review 的 `needs_human` run 不受影響。

合法且exact-bound的review `state=rejected`會保存immutable GateEvaluation、把當前card標成`needs_human`並停在原phase；periodic runner不得重派。只有operator explicit `cortex work resume`可在Candidate、report與evaluation hash重驗後建立fresh reviewer Job。Blocking category只描述Candidate或acceptance缺陷；若只是前份review report的措辭／列舉精度且不改變Candidate verdict，fresh reviewer應以non-blocking `style`留下更正，不得冒充Candidate correctness。

若合法`state=passed` review evidence已canonical bind，但step audit或registry save在完成前中斷，operator resume會重驗同一份exact evidence並冪等重播，不建立fresh reviewer Job；forged、stale或unknown state仍停在`needs_human`。

## Snapshot 與 registry migration

- Work snapshot：`$PSC_MONITOR_STATE_ROOT/work-items.snapshot.json`；未設定時為 `$PSC_AGENTS_ROOT/monitor/work-items.snapshot.json`。
- Installed service 先依 unit 宣告順序合併 `<instance>.env` 與 `<instance>-manager.env`；預設 socket 為 `$PSC_AGENTS_ROOT/run/<instance>/project-monitor.sock`，`monitor.socket_path` override 優先。
- `doctor --probe-live` 必須以 production Monitor config 解出 socket，再用 read-only `list_work_items` 驗證 `ok` 與 `cortex-work/v1` envelope；裸 listener 或只完成 connect 都視為失敗。Identity registry若配置Claude `review` capability，doctor亦把Claude Code版本/CLI surface、`bubblewrap`、`socat`、`srt`、live native與Unix-socket seccomp smoke列為required probe；未配置Claude reviewer時只回非必要warn。
- Snapshot schema：`work-items-snapshot/v1`，mode `0600`，atomic replace + file/directory fsync。
- Coordinator registry：首次載入合法 v1 時先建立 read-only、content-hash 命名的 backup，再升級為 v2。
- 舊 jobs/slices 只進 `legacy_records`，不會猜測 work item association。
- Unknown/malformed schema 不會覆寫現有合法檔案；先修復或從已驗證 backup 恢復，再 restart service。

## Delivery gate

Manager 是唯一 writer。每次 push 都會使上一個 delivery review epoch 失效，並重新要求 current-HEAD review。第一次 ship preflight 前，Manager 會在自己的 ship clone 內以 bounded direct git subprocess probe `origin/main`：依序 resolve/validate Candidate、fetch、resolve/validate `FETCH_HEAD`、計算 merge-base，必要時再跑 `merge-tree --write-tree --name-only --no-messages -z` 與 NUL-safe path parser。只有 C 已含最新 M 時才可進 preflight；clean-behind 與 conflict 一律在 preflight／push／建 PR 前停下。fetch／merge-base／merge-tree／path-parser failure 會把 `candidate`／`stage`／`returncode`（timeout 為 null）／`error_kind`／`main_head` 寫成 content-addressed `main-sync-probe` evidence；若真的要 push，preflight 後、`git push` 前還會再 probe 一次。已進 merged/done closure 的 reconciliation 與 terminal refresh 則直接走既有 closure path，不重跑 main probe。Merge 前必須同時具備：

**main-sync stop 的人工 Builder 出口（#943／#972／#989／#990）。** clean-behind 或 conflict 轉成 `delivery-needs-human` 後，status 只有在 exact Candidate 與保存的 C 相符、main SHA 合法且 evidence ref 已保存、build steps 全部 passed、沒有 active job，且 passed ship step 僅可能是 Manager 的 `openspec-archive` 時才列出 `retry-build` 與 exact-Candidate 指令。此 action 沿用 registry reset 和 operator adjudication；Builder 會收到原 stop evidence 與 probe 記錄的 main SHA，產生修復候選後由既有 verify／review gates 重跑，ship probe 再確認同步才繼續。probe unavailable、context 缺漏或 C 不符時不列出這個 retry。

- exact tree 的 policy + pinned preflight；
- deterministic verification 與不同 independence domain 的 ForeignReview；
- 恰好一種 current-HEAD typed delivery review：非 error 且 threads resolved/outdated 的 Copilot review，或 immutable exact-HEAD maintainer attestation；
- terminal-green checks/statuses、closing refs、archive diff 與 mergeability；
- fresh GitHub provider snapshot。

同一 exact HEAD 的 Copilot review 若因 finding 停在 `needs-fix`，operator 可用 `cortex work review-disposition <work-id> --repo <owner/repo> --actor <operator> --reason <理由>` 記錄明確續行裁決。Manager 會重新驗證 PR HEAD、latest Copilot review 與所有 review threads，並將 actor、reason、finding 與 thread snapshot 寫入 immutable evidence；只有現況仍與裁決綁定一致且 threads 全部 resolved 才接受。之後需明示 `cortex work resume`，照常重跑 delivery gates；finding 與 disposition 歷史會保留。單獨 resolve thread 不會授權 merge，HEAD／review／thread snapshot 改變或仍有未解 thread 時仍會阻擋。

最多兩輪 builder fix/re-review，每個 HEAD 等待 15 分鐘；對 request-bound Copilot review，deadline 以 review `submitted_at` 相對 request epoch 計算，Manager 輪詢較晚才觀測到 review 不會重設或延長這 15 分鐘，沒有 review 或 review 真正晚於 `requested_at + 900` 才算 timeout。既有 exact-HEAD Copilot review 直接採信。若 `copilot-review-timeout` 停在**舊** HEAD，而 canonical run 的 current verified Candidate 已前進到另一顆 exact HEAD，只有 operator 明示 `resume` 才能建立一次性的 Manager-owned rearm permit：`_ship_action` 會先重讀 current candidate／preflight tree／delivery binding／PR HEAD，並要求 checks 終態通過、PR 可合併且 current threads 無未解，條件全數相符才把舊 timeout/review epoch append 到 `delivery_review_epochs`，再以新 HEAD 進入 `review-requesting` → `review-requested`，或直接採信已存在的 exact-HEAD Copilot review。same-head timeout、沒有 permit 的 replay，或 `review-requesting` 之後仍無法證明 request outcome（API/HEAD race、crash window）都不會重送；該 epoch 會以 `copilot-review-request-outcome-unknown` fail-closed，等待人工處理。`copilot-*` stop 仍可 `review-attest` 重入；current-HEAD review 出現 finding 時，delivery adapter 會把 `fix-required` fail-closed 投影為 `needs_human`，只有 operator 的 exact-Candidate `retry-build` 才能重開 builder；第三次仍有 finding 或逾時也維持 `needs_human`。若後續已由Manager綁定exact-HEAD maintainer evidence，只有完整path/hash可重入這類`copilot-*` stop，其他stop reason仍fail-closed。同一 run/head 若先前已有 Copilot v1 merge authorization，maintainer fallback 會把該 immutable v1 保留為 superseded 稽核，另建 content-addressed v2 並在 v2 內綁定 v1 ref/hash；這個 superseded 驗證只對 audit-only 的 v1 放寬 authority_digest，不授予額外 merge 權限，`merge-authorized` 與 merge 前 gate 仍不使用 terminal authority 例外。合併只使用 `gh pr merge --merge --match-head-commit <HEAD>`，不使用 auto/squash/rebase。Merge 後會重新 fetch default branch，驗證雙親 merge commit ancestry、issue、mapped Todo 存在且內容可讀，以及 CompletionRecord；workstream Todo 未勾 checkbox 只作觀測，不阻擋 remote closure。若 work item 有 mapped OpenSpec，仍要求 archive 成立、active OpenSpec 消失，且 archived tasks 通過既有 archive gate。`mapped_openspec == ()` 時不要求 OpenSpec archive；未勾 Todo checkbox 仍不阻擋有效 CompletionRecord 的 closure。CompletionRecord會保留實際使用的`copilot`或`maintainer-review` kind/ref/hash，並要求恰好一種delivery review authority。Completion Draft以排除`completed_at`的normalized closure語意hash版控：同語意重試沿用首份immutable draft，default branch或authority前進則建立新revision並保留舊檔，任何malformed collision都拒絕覆寫。Verify與ForeignReview原始canonical envelope會先依各自per-card slice完整重驗，再以共同WorkflowRun ID派生只供closure使用的evidence，使CompletionRecord strict reader可交叉驗證slice、Candidate與builder/reviewer jobs；原始證據不改寫。若post-archive retry-build產生descendant final Candidate，ship audit只對registry仍標示passed的Manager archive job接受Git驗證過的ancestor；未宣告 `openspec-archive` step（如 `fix-standard`）的 combo 也以這筆唯一 Manager archive job ship evidence 判定 archive-applied；policy-commit仍須exact final Candidate，unrelated commit或ancestry錯誤全部拒絕。此時active OpenSpec planning path已由official archive移走、issue/PR/archive狀態使WorkAuthority digest前進都屬預期terminal transition；完整綁定`merged`或cached `done` Candidate/merge commit/authorization的journal會直接進入ship validator closure，涵蓋validator已完成但WorkflowRun finalization尚未落盤的crash window。若default snapshot在此期間前進，Manager會提供current semantic draft，validator以它完整重驗成功後更新journal的CompletionRecord；沒有replacement時仍重驗cached record。Immutable authorization保留merge當下的digest而其他binding仍完整重驗，不回退要求active path；merge-authorized與merge前gate完全不使用此例外。全部remote facts成立才投影 `done`。

V1 terminal delivery 僅支援 GitHub。其他 forge 仍可顯示 read model，但 ship 會停在 `needs_human`。

Authority 前進時，claim 會先讀取收到的 delivery journal；若 review run 的完整 merge authorization、workflow step、PR binding 與 Candidate 都相符，便保留原 phase，不執行 authority-restart reset。舊版已標記 `retry_classification=authority_restart` 且 reset 到 `verify` 的 merged run，`resume` 會 fail-closed 回報 `merged-run-reset-to-verify`、不派 verify，並提供 `cortex work <work-id> retire-delivered --repo <owner/repo> --expected-run-id <run-id> --actor <operator> --reason '<single-line reason>'`。`retire-delivered` 只退休孤兒 run，維持 abandoned/superseded 語意；它不補寫 CompletionRecord 或 shipped outcome，也不把 run 標成 done。

## Terminal lifecycle canary

`terminal-lifecycle-canary` confirmed mapping 對應 issue #31。這條 docs-only canary 保持 persona-domain separation：primary `planner` 必須整合 `agy/google` evidence 完成 heterogeneous brainstorm，`planner`、`builder` 與 `reviewer` 則分別在不同 independence domain 產出規劃、最小文件 diff 與獨立審查 evidence。

候選變更必須通過 OpenSpec validation、policy、full preflight、ForeignReview，以及 exact current-HEAD 的 adversarial maintainer review。任一 typed output 或 gate 缺失、失敗或無法對準同一 HEAD 時，workflow 必須 fail-closed 保持 `needs_human`，不得宣稱 terminal completion。

PR #54 僅識別目前仍為 open 的 delivery target；此編號本身不是 merge、issue closure 或 `done` evidence。Manager 先以 official archive 流程封存 OpenSpec change，後續只有在其餘 strict gates 通過後，才可透過該 PR 以帶 closing reference 的 merge commit 交付並關閉 issue #31。重新讀取 default branch 與 remote authority 後，只有 PR、archive、merge ancestry、issue closure、Todo 與 CompletionRecord 全部成立，Monitor 才能投影為 `done`。

## Terminal/result contract（#261）

**#803 verification full-suite 證據**：Manager 只在同一 run 的成功 build job ledger
之 `worktree_state.head`、build job `subject_head` 均符合目前 Candidate，且 ledger
含 `pytest` 結果時，才把路徑、canonical sha256、命令、exit code 與摘要附入
verification prompt。verifier 仍須檢視 diff 並跑 focused／diff 相關測試；唯讀 sandbox
中的 ACL／xattr、唯讀檔案系統或過長 `TMPDIR`／AF_UNIX `sun_path` 環境失敗，不得覆蓋
相符 ledger 已證明的 full-suite 綠燈。ledger 記錄失敗時，focused 綠燈也不能覆蓋；ledger
缺席或 Candidate 不符時維持原判準。此 ledger 只證明 full-suite gate，不會自動授權
verification 通過。

`paulsha_cortex/coordinator/terminal_contract.py` 是 terminal/result 契約的單一真相源，供 build、verify、review 三類 card 共用。

**Canonical envelope。** envelope 帶 `schema_version`，並完整支援 `passed`、`failed`、`needs_human` 三種終局狀態與結構化 `diagnostics`；三類 card 都不存在「只有成功形狀才合法」的路徑。不帶 canonical 版本的舊 payload 走相容讀取路徑並記 legacy 標記，既有 run 不因版本差異被拒收。

**Terminal JSONL extraction 與 recovery 邊界（#860）。** Manager 讀取 terminal log 時以保留換行的 UTF-8 reader 開啟檔案，且只用 literal LF（`\n`）切分 JSONL records。CRLF 是相容輸入；單筆 JSON 外的 CR 仍交給 JSON whitespace／既有 fence 規則處理，但裸 CR 不會被當成另一個 record delimiter，因此兩筆 JSON 只以裸 CR 相接時會 fail closed。末筆無換行、空行與尾端空行都不改變這項判定。Recovery 只讀取既有 log；路徑遺失、檔案遺失、UTF-8 無效、純文字或 terminal shape 無效時，不會產生 terminal evidence。

JSON string data 中的原始 Unicode NEL（`U+0085`）、line separator（`U+2028`）與 paragraph separator（`U+2029`）會保留在 details／reports 等欄位；inner `result` JSON string 與 outer provider record 的 `structured_output` 各自可能採 raw 或 `ensure_ascii` escaped 形式。只 escape inner result 並不足以修復會把 outer raw separator 當換行的 reader；以 ASCII codepoint 名稱暫避只是一種內容改寫，不能當作 fidelity 修復或事故 replay 的替代品。Parser 仍只接受既有 terminal carriers 與一層白名單 wrapper；任意 `structured_output` 不會自動成為證據來源。這項修復也不把 generic top-level text 欄位變成經 event-type 認證的工具輸出，既有相容路徑的限制仍須分開看待。既有 `work start`／`recover work` CLI 介面不因本修復新增旗標。

**gate ledger 由 manager 產生，不是模型自述。** 重驗只有在「被驗的東西不是模型講的話」時才有意義。`launcher.build_wrapper_script` 產生的 headless wrapper 是 manager 擁有的，形狀為：

```text
<模型 argv>; __psc_rc=$?; <gate ledger writer>; printf %s "$__psc_rc" > <sentinel>; exit "$__psc_rc"
```

各段以 `;` 串接，因此模型失敗時 gate 仍會執行並產生 ledger。wrapper 先保存模型 exit code，gate writer 結束後才寫 sentinel；sentinel 因此代表 gate 階段已完成，模型 exit code 也不會被 gate 耗時或結果污染。gate 階段輸出導向 `/dev/null`，不污染 JSONL 的 terminal evidence。gate 清單由 operator 以 `PSC_GATE_CMD_<NAME>` 環境變數宣告（沿用 `PSC_PREFLIGHT_CMD` 的 typed-argv 規範，拒絕 shell wrapper），exit code 由真實 subprocess 產生。模型既不能選擇跑哪些 gate、不能決定 exit code，也拿不到 ledger 路徑（`<log_dir>/<slice_id>.gates.json` 由 job 的 `log_path` 推導，模型的 cwd 是 worktree）。跑不起來或逾時的 gate 一律記為 `failed`，避免 operator 設定壞掉靜默變成 fail-open。

**成功必須被證明。** `manager.terminalize_workflow_job` 在任何狀態採信之前，先以 `_assert_terminal_gate_consistency` 做確定性 cross-check：只要 ledger 中有任何 gate 的實際結果不是 passed，terminal 自稱的 `passed` 一律 fail closed，錯誤訊息保留哪一個 gate、期望值與實際值。「沒提到」不能當作「沒失敗」——terminal 完全不引用某個失敗的 gate 也一樣被否決。ledger 自身矛盾（記了非 0 exit code 卻標 passed）視同失敗。envelope 內的 `gate_evidence` 是模型「自述跑了哪些 gate」的宣告，manager 以 ledger 對照：宣稱跑了 ledger 中不存在的 gate，或宣稱的結果與 ledger 不符，皆 fail closed。會實際跑並要求本卡 gate ledger 的 phase（`build`，見 `GATE_LEDGER_REQUIRED_PHASES`）若連 ledger 都不存在，代表 wrapper 的 gate 階段沒跑完，同樣 fail closed。模型輸出的自然語言、exit code 為 0、以及「沒有明確錯誤」三者皆不構成成功授權。

**operator 未宣告任何 gate 時的語意。** 沒有 `PSC_GATE_CMD_*` 時 wrapper 仍會寫出 `gates: []` 的 ledger：ledger 的**存在**證明 wrapper 跑完了，內容為空則代表 operator 明確選擇不設 gate。此時 `passed` 會被放行——這是 operator 的顯式設定，不是靜默旁路，但也表示此設定下沒有 R2 保護。要讓保護生效，至少宣告一個確定性 gate。

**`test_policy=red-required` 卡的語意反轉（#307）。** tdd-red 卡（`execution.test_policy=red-required`）的正確產出是「新增並 commit 會失敗的 RED regression test」；宣告 `PSC_GATE_CMD_PYTEST` 時，這張卡的 pytest gate *理應* failed，若照一般規則會與 terminal 自稱 `passed` 矛盾，結構性地讓這類卡永遠不可能通過。`_assert_terminal_gate_consistency` 因此會從 job 綁定的 `WorkflowRun.steps` 查出目前 card 的 `test_policy`，傳給 `terminal_contract.authorize_terminal`；只在 `test_policy="red-required"` 時，才對 ledger 中名為 `pytest`（`terminal_contract.RED_REQUIRED_TEST_GATE_NAME`）的那一項做反轉：exit code 精確等於 `1`（pytest 的 `TESTS_FAILED`：測試被收集、確實執行，且至少一個失敗）視為合格 RED 並反轉為 `passed`；exit code `0`（全綠，未產生 RED）反轉為 `failed`；其餘 exit code（`2`／`3`／`4`／`5`，對應 collection error／interrupted、internal error、usage error、no tests collected）維持既有的 `failed` 判定、不做任何轉換，避免「builder 根本沒寫測試」或「測試檔壞掉」被誤判為合格 RED。反轉只精準命中這一個 gate 名稱，其他 gate（`openspec`／`policy`…）與一般卡（`test_policy` 非 `red-required`）完全不受影響，仍走上一段的 fail-closed 規則；envelope 內模型自述的 `gate_evidence` 也刻意繼續對照未反轉的原始 ledger 事實，模型應誠實回報觀察到的結果（例如 `pytest: failed`），不需要也不應該自己做語意反轉。

**canonical gate 名稱由宣告機械產生進 prompt（#540）。** envelope 的 `gate_evidence[].name` 必須落在 ledger 實際跑過的 gate 名稱集合內，而那個集合完全由 `PSC_GATE_CMD_<NAME>` 導出（`PSC_GATE_CMD_PYTEST` → `pytest`）。派工 prompt 因此不得只寫「gate name」讓模型自由發揮——實測 tdd-red 卡的 builder 自報 `'focused pytest RED expectation'`，採信必然撞 `gate-evidence-unknown-gate`。`_workflow_job_prompt` 組 `terminal_schema` 時，以 `gate_ledger.ledger_gate_names()`／`gate_ledger.gate_evidence_name_hint()`（與寫 ledger 同一條導出路徑，同一份 env）機械產生 `allowed_names` 與說明文字，宣告改動自動同步到 prompt；`test_policy=red-required` 的卡另附 `red_required_policy`（由 `terminal_contract.red_required_status_hint()` 依 #307 的反轉判準產生），明示「ledger 顯示 pytest failed 時仍回 status=passed 且誠實自報 `pytest: failed`」，避免泛用 `status_policy` 的字面要求與實際採信規則相反。

**gate 的適用範圍同樣機械導出（#721）。** 名稱機械化之後仍留了另一半：`allowed_names` 與範圍紀律文字過去只由 `PSC_GATE_CMD_*` 全量導出，**與卡片的 `test_policy` 無關**，因此`test_policy=none` 的卡（例如 `worktree-isolation`）也被 prompt 告知「Manager 會重跑`python3 -m pytest -q` 並用它判你的 passed」——而 harvest 端的`terminal_contract.expected_gate_names_for_test_policy()` 對這張卡回的是空集合。兩端對同一張卡的答案相反，模型只能去跑 pytest，在 `-s read-only` 沙箱下必死並被自動重派。`_workflow_job_prompt` 現以 `gate_ledger.card_requires_gate_evidence()`（轉呼叫 harvest 端那支判準，prompt 端不寫第二份）決定範圍：契約不要求模型交出 gate 結果的卡，`allowed_names`為空、兩段文字逐字要求 `gate_evidence: []`，並改為據實說明「**不要你跑**，但 Manager 仍會自己跑宣告的 gate 並據以判定」——`gate_runner.ensure_gate_ledger()` 只看 phase 不看`test_policy`，這種卡照樣會被跑 gate、也照樣會被 ledger 裡的失敗 fail closed。其餘`test_policy` 的 prompt 逐字不變；收窄刻意只當布林用，不拿應驗集合去 ∩ ledger 名稱——後者會讓 prompt 講的判定範圍比 harvest 真正判的**窄**，正是同一個缺陷的鏡像。

**gate 宣告缺漏在開工前就被擋（#540）。** `cortex doctor` 的 `gate-declarations` probe 以 packaged deck 每張卡的 `execution.test_policy`（經 `terminal_contract.expected_gate_names_for_test_policy`，與 harvest 端同一個判準）導出應驗 gate 集合，比對 effective service env 的 `PSC_GATE_CMD_*` 宣告：宣告不合法、或未涵蓋應驗 gate 皆為 required fail，訊息直接給出缺哪個 gate 與範例宣告。過去這個落差只會在 builder 跑完、交付合格 candidate 之後才以 `gate-ledger-missing-expected-gate` 現形，且錯誤只進 `manager.log`。

**`regenerate-gates`：ledger 凍結後的官方重驗路徑（#540、#557）。** ledger 是 job 結束當下依當時 env 寫成的檔案，之後即凍結；operator 補上漏掉的宣告後，既有動作都到不了它——`resume` 只重讀同一份舊 ledger 再拒一次，`retry-build` 只受理最後一張 builder 卡（tdd-red 是中段卡），`recover-pre-candidate` 要求 null candidate。`cortex work regenerate-gates <work_id> --repo owner/repo --expected-run-id <workflow-...> [--card <card-id>]` 以 exact WorkflowRun CAS 定錨；指定 `--card` 時只從該 build 卡的終止 job 中取最後一筆，未指定且只有一張符合條件的 build 卡時沿用取最新 job 的行為，若有多張則 fail closed 並要求 `--card`。gate 依**當前**宣告重跑並原子覆寫所選 job 的 ledger。它刻意只做這件事：不重派任何模型、不動 builder 的 commit、不改任何 run/slice 狀態、不直接改判——run 仍停在 `needs_human`，採信與否由既有的 `resume` → harvest 流程重新評估。fail closed 條件為 run 必須 ongoing 且帶 `needs_human`、CAS 必須精確相符、必須真的找得到符合條件的 job log 與 worktree、gate 宣告必須合法；任一不成立即拒絕且不產生任何 side effect。

**`retry-card`：中段 builder 卡的重派路徑（#545）。** ledger 重生成之後，還有一種卡死形態是**舊 job 的 terminal envelope 本身不可用**——envelope 是模型輸出、契約內不可竄改，實測 tdd-red 的 builder 自報 gate 名 `'focused pytest RED expectation'`，`regenerate-gates` 把 ledger 修對之後 `resume` 仍必敗於 `gate-evidence-unknown-gate`。唯一乾淨出路是以修好的 prompt（#540 已把 canonical gate 名機械注入 `allowed_names`）重派那張卡產生**新** envelope，但既有動作都到不了：`retry-build` 只受理最後一張 builder 卡（tdd-red 是中段卡），且它是 candidate 修復語意——會把該卡的 `action` 覆寫成 repair 文案，中段卡走那條路等於把「寫一個 RED regression test」這個指示抹掉；`recover-pre-candidate` 要求 null candidate；`abandon` 會連合格的 RED commit 與一個世代一起燒掉。`cortex work retry-card <work_id> --repo owner/repo --expected-run-id <workflow-...> --card <card-id>` 以 exact WorkflowRun CAS 加上卡名定錨，原子清掉 `needs_human` facet 並讓 manager 以**原卡片契約**重派一個新 job（prompt 走既有的 `_workflow_job_prompt`，沒有第二條組裝路徑）。舊 job 與舊 envelope 一個位元組都不動，原樣保留供稽核；重派只允許產生新 job 與新 envelope。fail closed 條件為 run 必須 ongoing、帶 `needs_human`、在 build phase、無 active job，`--card` 必須**正是**下一次 dispatch 會派的那一張（build phase 內最早一張非 passed 的 builder 卡，與 `manager._current_workflow_step` 同一判準），該卡不得已綁定 `workflow_evidence`（已採信的 evidence immutable，不得以重派名義覆寫），且該卡必須已有一顆終止的 job（從未派過的卡屬 `resume` 的職責）。dispatch 若失敗，`needs_human` 會被補回去，不留下「facet 清了但沒派出去」的中間態。

**`retry-card` 的 per-card 重派熔斷（#555）。** 同一張卡最多接受三次 operator 明示重派，次數持久化於 `WorkflowRun.attempts["retry-card:<card>"]`；升級前的既有 run 則以該卡已派出的 job 歷史補算，避免從零重新給額度。超過上限時 action 不會重派，run 維持 `needs_human`，`blocking_reason` 會指名卡片與上限；`next_actions` 不再列出 `retry-card`，通用出口為 `abandon`，符合既有 admission 條件時仍可使用 `retry-build`。

**`retry-card` 一併涵蓋 reviewer 卡（#569）。** 同一個 run 的 verify 階段之後重現了同型死結：verification job（agy，#568 的權限剖面缺陷）exit 0 但 log 完全沒有 JSON envelope，harvest 每次都撞 `workflow terminal log has no JSON evidence`。reviewer 卡當時沒有等價出口——`retry-verify` 是 slice-lane 時代的 **phase 級**重置，它清掉 `needs_human` 與 verify step 卻**不在同一個 action 內派新 job**（實測回應 `job: None`），run 因此四小時對 tick 隱形後 `needs_human` 原地回鍋。`retry-card` 的受理範圍因此放寬到 verify／review phase 的 reviewer 卡（`verification`／`code-review`／`adversarial-review`），CAS、卡名定錨、evidence immutable、facet 原子性、舊 job 不動等硬約束逐條沿用：`--card` 必須正是當前 phase 內最早一張非 passed 的卡且 persona 與 phase 相符；已對**現在這個 candidate** 綁定 `workflow_evidence` 的卡拒絕重派（上一代 candidate 的歷史 evidence 不參與判斷，否則換過 candidate 之後會再造同一個死結）；新 job 的身分由 identity registry 在 dispatch 當下**重新解析**，不複製舊 job 的 executor／model（#568 的 reviewer fail-over 依賴這一點）；被取代 job 的 reviewer sandbox 在派新 job 前原子回收，回收時若發現 candidate checkout 已被改動則 fail closed。`retry-verify`／`retry-review` 的 CAS 與 admission 一字未改（`docs/superpowers/specs/fix-repair-commit-recovery-spec.md` R4）。兩者 reset 標記舊 exited reviewer job 為 failed 前，Manager 會先套用精準 terminal recovery 判準；符合者保留 `exited`，讓後續 explicit resume 仍可採用該復原。

**reviewer sandbox 目錄名綁 job（#579）。** 新目錄以 `sha256(run_id:card:candidate:job_id)[:32]` 命名；升級前已派出的 job 仍可讀取 legacy 名稱。派出新 reviewer job 前，Manager 會回收前代 claim era 中已終止且未與本 era 共用 worktree 的孤兒 sandbox；單筆回收失敗只記 warning，不阻擋派工，因此 authority restart 後重派不會因舊 sandbox 撞名而停住。

**verify／review 的誠實 non-passing terminal 直接落明示停止（#874）。** reviewer card 若交出形狀合法的 `failed`／`needs_human` terminal，Manager 不再把它包成 `terminalize-workflow-job-failed`／`resume-workflow-failed`；run 會直接進 `needs_human`，reason 分別為 `verification-terminal-explicit-stop`／`review-terminal-explicit-stop`，detail 保留模型原文，`evidence_refs` 直接指向該 job 的 terminal log。這條路不自動重派、periodic runner 不會偷 re-dispatch；operator 顯式 `resume` 也只會重讀同一顆 terminal 並重落同一個停止。真正的重派出口仍是 `retry-card`／`retry-build`。

**needs_human 的 next_actions 會反映該 phase 的實際出口（#546 部分、#569 一般化）。** `claim._resume_decision` 只看得到 run 的 phase 與 planning failure 記錄，卡片卡住時它宣告的唯一出口是 `abandon`——#569 的 operator 正是因此改用只重置不重派的 `retry-verify`。work action 層（拿得到 JobRegistry）另以與各動作**完全相同**的前置驗補算 `regenerate-gates`／`retry-card` 是否真的會被受理，是才加進 `resume` 回傳的 `next_actions` 與 `cortex status` 的 attention 條目——只宣告會成功的動作，拿不準就不宣告。對 `blocking-findings` 的 review run，符合各自前置條件時再依序宣告 `retry-review`（接受裁決）／`retry-build`（駁回裁決），並附上可照抄的指令提示；`list_jobs()` 讀取失敗時不宣告這兩個出口。`review-attest` 對 rejected review gate fail closed。build／verify／review 三個 phase 共用這一份判準。

**request 完成 ≠ Job 啟動 ≠ phase 推進：派工結果的四類契約（#830）。** `work start`／`intake`／`resume`／`retry-*` 與 periodic continuation 送到 Manager 的派工結果只有四種：**真 Job**（回 `job_id`，且該 Job 在 registry 中綁定到同一個 run；forged job_id 或綁到別的 run 一律拒絕）、**合法非 Job 決策**（producer 回 `{run_id, current_phase, reason}`，例如 sizing_band=red 的 `needs-decomposition`、`runtime-preflight-*`、`plan-outputs-missing`；不造 job_id、不算 dispatched、不啟動模型）、**確定性 transition**（沒有 Job 但 run 已持久化推進 phase）、**None**（沒派也沒推進，維持 not-dispatchable）。request 回應以 `dispatch.kind` 呈現這四類並附最新持久化的 run；真 Job 才另帶 `job_id`。合法決策不會因 adapter 例外被降成一般 `needs_human`／`resume-workflow-failed`，重送同一 start 也不會新增 run 或 Job；forced retry（`retry-build`／`retry-card`）的成功後置條件仍是「新 replacement Job」，遇到決策或 None 一樣走既有 fail-closed 補償並把 producer 的 reason 帶進診斷。
**dirty recheck 是輪詢，不是 lifecycle transition（#496）。** `complete_tick` 對停在 `needs_human` 且 verification summary 為 `candidate-worktree-dirty`／`candidate-worktree-dirty-after-verification` 的 slice 每 tick 重驗一次，operator 清乾淨 worktree 後不需任何 action 就會脫困。重驗**本身**不是狀態轉換：結果與 slice 目前的 evidence hash（`current_verification_evidence_hash`，#501 起與 contract hash 分離）、state／gate_state、candidate、summary、current evidence refs 完全相同時，Manager 不寫 action、不追加 `evidence_history`、不 `update_slice`；只有任一欄位真實變更（含同路徑不同內容、hash 缺失或 refs 指向別處這類需要修復的情況）才記恰好一筆，下一次相同結果即 no-op。因此 `evidence_history`／`actions` 的每一筆都代表一次真實 transition，可直接當稽核用，不再混入每 tick 的輪詢噪音（舊實作一個放著不管的 dirty slice 六天累積 92k 筆 `verification-failed`）。新 evidence 不可讀、hash 或 payload 不符時維持既有 fail-closed，不視為「未變」也不放行。

**slice-lane builder 拿到的是 pinned spec 逐字內容，且由 Cortex 自己 attest（#503）。** 舊 prompt 只有 `[TASK] <slice_id>` 與 `[PLAN: path]`，controller 重釘 spec 後補進的 recovery 指示與必做回歸測試在模型邊界被靜默丟掉，registry 卻顯示新 spec hash——稽核只證明「選了哪個 hash」，不證明模型讀到了它。現在 dispatch 在登記 slice row 之後重讀 spec，其 sha256 必須等於 pin 值（不等或不可讀就對該 slice fail-closed 成 needs_human、不派 job），prompt 附 `[SPEC: path sha256=…]`、固定明示語句與逐字 `[SPEC BODY]`（超過上限截斷並指示讀檔），builder 角色缺 spec 即拒絕組 prompt；job row 記錄實際交付的 `spec_hash`／`plan_hash`，完成側除了檔案漂移檢查，另比對 builder job 記錄的 hash 與 slice 釘住的值（`builder-input-spec-hash`／`builder-input-plan-hash` → pinned-input-mismatch，candidate 不得通過）；foreign review prompt 也附同一份 `[SPEC …]` 行。
**operator 裁決（`--reason`）是必須執行的指令，不是 metadata（#814）。** `retry-build`／`retry-card`／`retry-review` 帶 `--reason` 會落一筆 immutable `cortex-operator-adjudication/v1` evidence，完整保存最多 4000 字。Manager 於該 run 之後每一次 dispatch（builder 與 reviewer 皆然，#757）把最近 ≤3 筆放進 contract 的 `operator_adjudications` 區塊；每筆 reason 只注入前 2000 字，超出部分仍保留在 evidence。builder directive 要求實作適用裁決；reviewer directive 將裁決作為唯讀驗收判準，若裁決明示接受或豁免某 finding／偏離，不得再以 blocking 類別回報該 finding；仍需記錄時用 non-blocking 類別並在 recommendation 引用裁決。CLI 回傳的 `adjudication.next_step_hint` 會告訴 operator 裁決已記錄、將於下一次哪張卡 dispatch 注入；不需要再繞 reviewer findings 轉述一輪。

**升級與運維。** 派工 prompt 現在發的是 canonical envelope（`schema_version: 2`，多帶 `diagnostics` 與 `gate_evidence`）。不帶該版本的舊 payload 仍走相容讀取路徑，不會因版本差異被拒收。切換當下已在飛行、且沒有 gate ledger 的 build／verify run，其 `passed` terminal 會 fail closed 並轉 `needs_human`——這是預期行為（沒有獨立證據就不放行），不是資料損毀：candidate 與 worktree 都還在，只是未被授權。處理方式是對該 run 重新派工該張 card（resume 會以新的 wrapper 重跑並產生 ledger），不需要 abandon 整個 work item；只有在 candidate 本身已被判定不可用時才需要 abandon 重跑。

**schema mismatch 是有上限的確定性失敗。** StructuredOutput 的 wrapper 正規化只認明確白名單外層鍵（`input`／`params`／`parameters`／`arguments`／`payload`／`response`），且同一個確定性 mismatch 只嘗試一次修復；未知形狀終止為帶 machine-readable validation errors 的可操作錯誤，不以寬鬆解析吞掉未知欄位。`resume_workflow_run` 的 malformed-terminal 重派帶上限與計數器：計數持久化於 `WorkflowRun.attempts["schema-mismatch:<card>"]`，逾限即停止重派、轉 `needs_human`，並在回傳結果上曝光 `schema_retry_count`、`schema_retry_limit`、`last_validation_path` 與 `last_validation_reason`。計數同時經 workflow provider 的 observations 投影到 Monitor work item envelope，`cortex inspect work <id>` 會在發生過 mismatch 時列出 `schema_retry[<card>]: <count>/<limit>`（逾限標 `(exhausted)`）。計數刻意存放在既有的 `attempts` 欄位而非新增 `WorkflowRun` 欄位——新欄位會落在 `providers._WORKFLOW_V2_OPTIONAL_ROW_KEYS` 白名單之外，使每一個 run row 被判為 unsupported、整份 workflow projection 變 `degraded`（#205 曾實際踩到）。

**診斷與授權分離。** terminal parse 失敗時，`_terminal_parse_diagnostics` 保留 observed HEAD、job id 與失敗原因的唯讀診斷（`terminal_diagnostics`），但該 payload 明確標示 `authority_granted: false`，且不含任何 candidate authority 欄位——可觀測不等於可授權。

**provider／launch 失敗詞彙維持分層，且只對有證據的 executable 問題 reroute（#826）。** 結構化 terminal 與 controller interruption 仍優先於文字關鍵字；其後才接受可證實的 `exit 127` 空輸出，最後才走 provider text 的 `rate limit → quota → auth → effort_not_supported → executable_not_found → content → transient` 次序。`not found` 只有在已知 launcher／executor 的 shell `command not found` 上下文、同一行帶 `exec`／`execvpe`／`execve`／`spawn`／`Popen` 與 `No such file or directory` 的 launcher ENOENT（若同一行也附帶 `: <target>`，該 target 必須是已知 launcher executable，且不能只是缺失的 cwd/path；若沒有明示 target，則 bare launch-call ENOENT 仍可成立；未知 explicit target 一律維持 non-reroutable）、可信的空 `127`（不是缺 log／讀不到 log），或 launch 例外可證明缺的是 provider executable（如 `copilot`／`codex`／`claude`／`agy`／`cg` 或精確 executor 名）時，才會進 `executable_not_found`；HTTP 404、model not found、一般工作檔案 `No such file or directory`，以及 `bash`／`sh`／`git`／`systemctl`／`systemd-run` 這類 shared launch infrastructure 缺失，都不會借題發揮成 reroute。新的環境類詞彙是 `effort_not_supported`／`executable_not_found`／`launch_failed`：前兩者在 authority 不是 `hint` 時可有界 reroute，`launch_failed` 只保留原始 exception／缺 handle 診斷，三條 launch-failure producer path 都會同步保存 matching `launch-failed` runtime diagnostic，不自動 retry 或 reroute；真正的 `runtime-contract-failed` 與 reviewer candidate drift 仍比 provider routing 更強，持續 fail-closed。

**#582 sandbox 工具中止分類**：終局 `subtype=error_during_execution` 且 `terminal_reason=aborted_tools` 表示工具鏈被外部生命週期中斷，分類為 `environment`／`tool_aborted`，可進入 bounded retry；這不同於維持 `unknown` 的一般 controller interruption。
